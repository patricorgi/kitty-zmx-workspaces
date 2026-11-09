import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

from .core import FOCUS_PREFIX, atomic_write, check_name, focused_pane, pane_map, rewrite_export, saved_focus


def state_dir(name):
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    path = root / "kzw" / check_name(name)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def query(socket, *args):
    return subprocess.run(["kitty", "@", "--to", f"unix:{socket}", *args],
                          check=True, capture_output=True, text=True, timeout=5).stdout


@contextmanager
def capture_lock(directory):
    with open(directory / "capture.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def capture(directory, executable, allow_removals=False):
    with capture_lock(directory):
        return capture_locked(directory, executable, allow_removals)


def capture_locked(directory, executable, allow_removals=False):
    socket = directory / "kitty.sock"
    before_tree = json.loads(query(socket, "ls"))
    before = pane_map(before_tree)
    focus = focused_pane(before_tree)
    export = query(socket, "ls", "--output-format=session")
    after_tree = json.loads(query(socket, "ls"))
    after = pane_map(after_tree)
    if before != after or focus != focused_pane(after_tree):
        raise ValueError("panes or focus changed while taking snapshot")
    snapshot = FOCUS_PREFIX + json.dumps({"pane": focus}, separators=(",", ":")) + "\n"
    snapshot += rewrite_export(export, after, executable)
    # Layout itself can change without the window IDs changing.
    if query(socket, "ls", "--output-format=session") != export:
        raise ValueError("layout changed while taking snapshot")
    target = directory / "workspace.kitty-session"
    metadata = directory / "panes.json"
    if metadata.exists() and not allow_removals:
        old = set(json.loads(metadata.read_text()))
        current = {pane for pane, _ in after.values()}
        if not old.issubset(current):
            raise ValueError("pane removal detected; keeping last complete snapshot (use save --allow-removals)")
    # IDs are also embedded in the session, so a failed metadata write cannot
    # cause a partially written Kitty session.
    atomic_write(target, snapshot)
    atomic_write(metadata, json.dumps(sorted(pane for pane, _ in after.values())) + "\n")
    return len(after)


def restore_focus(socket, pane):
    tree = json.loads(query(socket, "ls"))
    panes = pane_map(tree)
    matches = [window_id for window_id, (name, _) in panes.items() if name == pane]
    if len(matches) != 1:
        raise ValueError("saved focused pane is missing or ambiguous; refusing to change focus")
    query(socket, "focus-window", "--match", f"id:{matches[0]}")
    if focused_pane(json.loads(query(socket, "ls"))) != pane:
        raise ValueError("Kitty did not restore the saved focused pane")


def detach(name):
    directory = state_dir(name)
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    # Serialize with the supervisor so it cannot overwrite the final snapshot
    # while the windows are being torn down. Failure to save means no close.
    with capture_lock(directory):
        count = capture_locked(directory, executable)
        query(directory / "kitty.sock", "close-window", "--match", "all")
    print(f"Detached {name!r}; saved focus and {count} panes")


def run(name, host):
    directory = state_dir(name)
    pane_executable = shutil.which("kzw-pane")
    if not pane_executable:
        raise ValueError("kzw-pane not found; install the project and activate its environment")
    if not shutil.which("kitty"):
        raise ValueError("kitty not found on PATH")
    if not host and not shutil.which("zmx"):
        raise ValueError("zmx not found on PATH; install it before starting a local workspace")
    with open(directory / "lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace is already running") from None
        sock = directory / "kitty.sock"
        # The lock proves no kzw supervisor is using this workspace. Refuse a
        # stale socket rather than unlinking anything we cannot verify is ours.
        if sock.exists():
            raise ValueError(f"socket already exists: {sock}; check for a running Kitty before removing it")
        snapshot = directory / "workspace.kitty-session"
        env = os.environ.copy()
        env["KZW_HOST"] = host
        env["KZW_FOCUS_SIGNAL"] = str(directory / "focus.changed")
        args = ["kitty", "--listen-on", f"unix:{sock}", "-o", "allow_remote_control=socket-only",
                "-o", f"shell={pane_executable}", "-o", "enabled_layouts=splits,tall,stack",
                "-o", f"watcher={Path(__file__).with_name('watcher.py')}"]
        focus = saved_focus(snapshot.read_text()) if snapshot.exists() else None
        if snapshot.exists():
            args += ["--session", str(snapshot)]
        else:
            args += ["--session", "none"]
        child = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL)
        print(f"Workspace {name!r} started. Keep this supervisor running; close Kitty to detach.", flush=True)
        last_error = None
        pending_focus = focus
        focus_signal = directory / "focus.changed"
        last_signal = None
        next_capture = 0.0
        try:
            while child.poll() is None:
                try:
                    if pending_focus:
                        restore_focus(sock, pending_focus)
                        pending_focus = None
                    try:
                        signal = focus_signal.stat().st_mtime_ns
                    except FileNotFoundError:
                        signal = None
                    if signal != last_signal or time.monotonic() >= next_capture:
                        capture(directory, pane_executable)
                        last_signal = signal
                        next_capture = time.monotonic() + 1
                    last_error = None
                except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                    message = str(exc)
                    if message != last_error:
                        print(f"kzw: snapshot deferred: {message}", file=sys.stderr, flush=True)
                        last_error = message
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nkzw: supervisor stopped; Kitty remains open, but snapshots have stopped.", file=sys.stderr)
            # Do not release the lock while Kitty still owns the workspace.
            child.wait()
        return child.returncode


def main():
    parser = argparse.ArgumentParser(prog="kzw")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="open a workspace and supervise snapshots")
    start.add_argument("name")
    start.add_argument("--host", default="", help="SSH host for newly created panes")
    save = commands.add_parser("save", help="save a live workspace explicitly")
    save.add_argument("name")
    save.add_argument("--allow-removals", action="store_true")
    det = commands.add_parser("detach", help="save the current focus and close the workspace view")
    det.add_argument("name")
    commands.add_parser("list", help="list saved workspaces")
    args = parser.parse_args()
    try:
        if args.command == "start":
            return run(args.name, args.host)
        if args.command == "save":
            executable = shutil.which("kzw-pane")
            if not executable:
                raise ValueError("kzw-pane not found on PATH")
            print(f"Saved {capture(state_dir(args.name), executable, args.allow_removals)} panes")
            return 0
        if args.command == "detach":
            detach(args.name)
            return 0
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kzw"
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if (path / "workspace.kitty-session").is_file():
                    print(path.name)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"kzw: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
