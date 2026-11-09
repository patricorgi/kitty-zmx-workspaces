"""One Kitty window attaches to one uniquely named zmx session."""

import argparse
import base64
import os
import re
import subprocess
import sys
import uuid

from .core import PANE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pane")
    parser.add_argument("--host", default=os.environ.get("KZW_HOST", ""))
    parser.add_argument("--existing", action="store_true", help="refuse to create a missing session")
    args = parser.parse_args()
    pane = args.pane or "kzw-" + uuid.uuid4().hex
    if not PANE.fullmatch(pane):
        parser.error("invalid pane ID")
    if args.host and (args.host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.host)):
        parser.error("invalid SSH host")
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
    if args.existing:
        remote += f'zmx list --short | grep -F -x -- {pane} >/dev/null || {{ echo "kzw: session {pane} is missing or unavailable" >&2; exit 1; }}; '
    remote += f'exec zmx attach {pane}'
    cmd = ["ssh", "-t", args.host, remote] if args.host else ["zmx", "attach", pane]
    try:
        if args.existing and not args.host:
            listing = subprocess.run(["zmx", "list", "--short"], env=env, capture_output=True, text=True, check=True)
            if pane not in listing.stdout.splitlines():
                raise RuntimeError(f"session {pane} is missing or unavailable")
        result = subprocess.run(cmd, env=env, check=False)
        if result.returncode:
            raise RuntimeError(f"attachment exited with status {result.returncode}")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"kzw: {exc}", file=sys.stderr)
        try:
            input("Press Enter to close this pane...")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
