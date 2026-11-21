"""A disposable local Kitty window while the remote attach is validated."""

from pathlib import Path
import subprocess
import uuid

from . import kitty_instance


CONNECTING = r'''
message="Connecting to $1 ($2)..."
set -- $(/bin/stty size 2>/dev/null)
rows=${1:-24} cols=${2:-80}
column=$(( (cols - ${#message}) / 2 + 1 ))
if [ "$column" -lt 1 ]; then column=1; fi
printf '\033[?25l\033[%d;%dH%s' "$(( (rows + 1) / 2 ))" "$column" "$message"
exec /bin/sleep 86400
'''


class AttachPreview:
    def __init__(self, name, host):
        self.shared = kitty_instance.context()
        self.nonce = uuid.uuid4().hex
        self.window = int(kitty_instance.remote_raw(
            self.shared["address"], "launch", "--type=os-window", "--tab-title=Connecting…",
            f"--var=kzw_attach_preview={self.nonce}", "/bin/sh", "-c", CONNECTING,
            "kzw-attach", name, host).strip())
        self.populated = False

    def command(self, action, value=""):
        kitten = Path(__file__).with_name("attach_kitten.py")
        return kitty_instance.remote_raw(
            self.shared["address"], "kitten", "--match",
            f"id:{self.window} and var:kzw_attach_preview=^{self.nonce}$",
            str(kitten), self.nonce, str(self.shared["pid"]), action, str(value))

    def close(self):
        if not self.populated:
            try:
                self.command("close")
            except (OSError, ValueError, subprocess.SubprocessError):
                pass  # Already closed, or no longer solely our preview.

    def error(self, exc):
        try:
            self.command("error", str(exc))
        except (OSError, ValueError, subprocess.SubprocessError):
            self.close()
