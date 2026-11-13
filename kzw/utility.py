"""Queue startup shared by managed sessions and legacy utility tabs."""

import os
import re
import shlex
import subprocess
import sys


def queue_bootstrap():
    return ('PATH="$HOME/.local/bin:$PATH"; '
              'if ! command -v fzf >/dev/null 2>&1 && [ -x "$HOME/.fzf/bin/fzf" ]; then '
              'PATH="$HOME/.fzf/bin:$PATH"; fi; export PATH; '
              'unset ZMX_SESSION ZMX_SESSION_PREFIX; '
              'for tool in condor_q fzf less date; do '
              'command -v "$tool" >/dev/null 2>&1 || { echo "Required command not found: $tool" >&2; exit 1; }; done; '
              'test -r "$HOME/dotfiles/scripts/tmux_condor_queue.sh" || '
              '{ echo "Queue picker not found: ~/dotfiles/scripts/tmux_condor_queue.sh" >&2; exit 1; }; '
              'exec bash "$HOME/dotfiles/scripts/tmux_condor_queue.sh"')


def queue_session_command():
    """Run inside the origin's zmx PTY, holding startup errors until Enter."""
    script = (shlex.join(["bash", "-lc", queue_bootstrap()]) + '; status=$?; '
              'if [ "$status" -ne 0 ]; then '
              'printf "Queue picker exited with status %s. Press Enter to close.\\n" "$status" >&2; '
              'IFS= read -r _ || :; fi; exit "$status"')
    return ["/bin/sh", "-c", script]


def queue_command(host):
    if not isinstance(host, str) or (host and (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host))):
        raise ValueError("invalid queue picker SSH host")
    command = ["bash", "-lc", queue_bootstrap()]
    return ["ssh", "-t", "-o", "ConnectTimeout=10", "--", host, shlex.join(command)] if host else command


def run_queue(host=""):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("KZW_") and key not in ("ZMX_SESSION", "ZMX_SESSION_PREFIX")}
    try:
        status = subprocess.run(queue_command(host), env=env).returncode
    except (OSError, ValueError) as exc:
        print("kzw: " + str(exc), file=sys.stderr)
        status = 1
    except KeyboardInterrupt:
        return 130
    if status:
        print(f"Queue picker exited with status {status}. Press Enter to close.", file=sys.stderr)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    return status
