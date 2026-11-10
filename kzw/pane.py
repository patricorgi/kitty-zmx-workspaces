"""One Kitty window attaches to one uniquely named zmx session."""

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .core import PANE, check_name


def set_attachment_ended(ended):
    encoded = base64.b64encode(b"1" if ended else b"0").decode()
    sys.stdout.write(f"\x1b]1337;SetUserVar=kzw_attach_ended={encoded}\x07")
    sys.stdout.flush()


def report_attachment_ended(retry=False):
    """Wait for reconciliation, or give it time before retrying SSH."""
    set_attachment_ended(True)
    threading.Event().wait(5 if retry else None)


def verify_workspace_lease(host):
    origin = os.environ.get("KZW_WORKSPACE_ORIGIN", "")
    if not origin and os.environ.get("KZW_WORKSPACE_COORDINATOR") != "1":
        return
    directory = os.environ.get("KZW_WORKSPACE_DIRECTORY", "")
    if host != origin or not directory or not Path(directory).is_absolute():
        raise RuntimeError("remote workspace identity is unavailable or inconsistent")
    from . import remote_client
    remote_client.renew_lease(Path(directory))


def reserve_new_workspace_pane(pane, operation):
    directory = os.environ.get("KZW_WORKSPACE_DIRECTORY", "")
    if not directory or not Path(directory).is_absolute():
        raise RuntimeError("remote workspace directory is unavailable")
    from . import remote_client
    remote_client.reserve_pane(Path(directory), pane, operation)


def journal_new_workspace_pane(pane, host):
    directory = os.environ.get("KZW_WORKSPACE_DIRECTORY", "")
    workspace = os.environ.get("KZW_WORKSPACE", "")
    if not directory and not workspace:
        return
    if not directory or not workspace:
        raise RuntimeError("workspace creation identity is incomplete")
    root = Path(directory)
    if not root.is_absolute() or root.is_symlink() or root.name != check_name(workspace):
        raise RuntimeError("workspace directory is invalid")
    pending = root / "native-pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    info = pending.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("unsafe native creation journal directory")
    path = pending / (pane + ".json")
    record = json.dumps({"pane": pane, "host": host, "workspace": workspace}, separators=(",", ":")) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_text() != record:
            raise RuntimeError("native creation journal conflicts with existing state") from None
        return
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(record)
        stream.flush()
        os.fsync(stream.fileno())


def wait_for_native_stage(pane, host):
    """Do not start a new session until its exact layout has been staged."""
    if os.environ.get("KZW_DEBUG") == "1":
        print("kzw: waiting for the workspace layout to be saved...", file=sys.stderr, flush=True)
    directory = Path(os.environ["KZW_WORKSPACE_DIRECTORY"])
    journal = directory / "native-pending" / (pane + ".json")
    ready = journal.with_suffix(".ready")
    stage = directory / "native-stage.json"
    expected_record = {"pane": pane, "host": host, "workspace": os.environ["KZW_WORKSPACE"]}
    signal = os.environ.get("KZW_CHANGE_SIGNAL")
    if signal:
        with open(signal, "a", encoding="utf-8"):
            os.utime(signal, None)
    while True:
        if journal.is_symlink() or not journal.is_file():
            raise RuntimeError("native creation journal disappeared before staging")
        text = journal.read_text()
        if json.loads(text) != expected_record:
            raise RuntimeError("native creation journal changed before staging")
        if ready.exists() or ready.is_symlink():
            if ready.is_symlink() or not ready.is_file() or ready.read_text() != hashlib.sha256(text.encode()).hexdigest() + "\n":
                raise RuntimeError("invalid native layout acknowledgement")
            if stage.is_symlink() or not stage.is_file():
                raise RuntimeError("native layout stage is unavailable")
            data = json.loads(stage.read_text())
            if (data.get("kind") != "kzw-native-stage-v1" or pane not in data.get("added", []) or
                    pane not in data.get("panes", [])):
                raise RuntimeError("native layout acknowledgement does not match stage")
            return
        threading.Event().wait(0.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pane")
    parser.add_argument("--host", default=os.environ.get("KZW_HOST", ""))
    parser.add_argument("--existing", action="store_true", help="refuse to create a missing session")
    parser.add_argument("--stage-new", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--initial-cwd", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    pane = args.pane or "kzw-" + uuid.uuid4().hex
    coordinated = bool(os.environ.get("KZW_WORKSPACE_ORIGIN") or os.environ.get("KZW_WORKSPACE_COORDINATOR") == "1")
    reservation = uuid.uuid4().hex if not args.pane and coordinated else None
    if not PANE.fullmatch(pane):
        parser.error("invalid pane ID")
    if args.host and (args.host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.host)):
        parser.error("invalid SSH host")
    if args.initial_cwd and (args.existing or not args.stage_new or not args.initial_cwd.startswith('/') or
                             any(ord(c) < 32 or ord(c) == 127 for c in args.initial_cwd)):
        parser.error("invalid initial working directory")
    for key, value in (("kzw_pane", pane), ("kzw_host", args.host)):
        encoded = base64.b64encode(value.encode()).decode()
        sys.stdout.write(f"\x1b]1337;SetUserVar={key}={encoded}\x07")
    sys.stdout.flush()
    env = os.environ.copy()
    # zmx uses this variable to switch an existing client instead of attaching.
    env.pop("ZMX_SESSION", None)
    env.pop("ZMX_SESSION_PREFIX", None)
    # Non-interactive SSH shells frequently omit ~/.local/bin from PATH.
    # The generated pane name is strictly validated, so it is safe to include
    # in this remote shell command without accepting arbitrary shell input.
    remote = 'PATH="$HOME/.local/bin:$PATH"; export PATH; unset ZMX_SESSION ZMX_SESSION_PREFIX; '
    existing = args.existing
    needs_journal = (not args.pane or args.stage_new) and bool(os.environ.get("KZW_WORKSPACE_DIRECTORY") or os.environ.get("KZW_WORKSPACE"))
    needs_stage = needs_journal
    needs_absence_check = not args.pane and not coordinated and not args.existing
    while True:
        remote_command = remote
        if existing:
            remote_command += f'zmx list --short | grep -F -x -- {pane} >/dev/null || {{ echo "kzw: session {pane} is missing or unavailable" >&2; exit 1; }}; '
        remote_command += f'exec zmx attach {pane}'
        local_command = ["zmx", "attach", pane]
        if args.initial_cwd and not existing:
            shell_command = 'cd -- "$1" || printf "kzw: cannot enter requested directory; using default\\n" >&2; exec "${SHELL:-/bin/sh}" -l'
            shell_args = ['/bin/sh', '-c', shell_command, 'kzw-shell', args.initial_cwd]
            remote_command += ' ' + shlex.join(shell_args)
            local_command += shell_args
        cmd = ["ssh", "-t", args.host, remote_command] if args.host else local_command
        failed = False
        attempted_attach = False
        try:
            if needs_journal:
                journal_new_workspace_pane(pane, args.host)
                needs_journal = False
            verify_workspace_lease(args.host)
            if reservation is not None:
                reserve_new_workspace_pane(pane, reservation)
                reservation = None
            if needs_absence_check:
                from .cli import zmx_command
                if pane in zmx_command(args.host, "list", "--short").splitlines():
                    raise RuntimeError("new pane ID already belongs to a live zmx session")
                needs_absence_check = False
            if needs_stage:
                wait_for_native_stage(pane, args.host)
                verify_workspace_lease(args.host)
                needs_stage = False
            if existing and not args.host:
                listing = subprocess.run(["zmx", "list", "--short"], env=env, capture_output=True, text=True, check=True)
                if pane not in listing.stdout.splitlines():
                    raise RuntimeError(f"session {pane} is missing or unavailable")
            attempted_attach = True
            result = subprocess.run(cmd, env=env, check=False)
            if result.returncode:
                raise RuntimeError(f"attachment exited with status {result.returncode}")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            failed = True
            print(f"kzw: {exc}", file=sys.stderr)
        retry = bool(failed and (args.host or needs_journal or needs_absence_check or needs_stage))
        if os.environ.get("KZW_DEBUG") == "1":
            print("kzw: attachment ended; checking whether the session still exists" +
                  ("; retrying connection" if retry else ""), file=sys.stderr, flush=True)
        report_attachment_ended(retry=retry)
        if not retry:
            return
        # A retry must never silently create a replacement for a lost session.
        if attempted_attach:
            existing = True
        set_attachment_ended(False)


if __name__ == "__main__":
    main()
