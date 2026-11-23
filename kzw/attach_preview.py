"""A disposable local Kitty window while the remote attach is validated."""

import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
import uuid


def splash_message(name, host):
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = config / "kzw/splash.txt"
    default = f"Connecting to {name} ({host})..."
    try:
        if not path.is_file():
            return default
        with path.open(encoding="utf-8") as stream:
            template = stream.read(8193)
    except (OSError, UnicodeError):
        return default
    if len(template) > 8192:
        return default
    values = {"workspace": name, "host": host}
    text = re.sub(r"\{(workspace|host)\}", lambda m: values[m[1]], template)
    # Allow line breaks and indentation, but never pass through terminal controls.
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")
    return text if text.strip() else default


def splash_screen(message, columns, rows):
    def width(char):
        if unicodedata.category(char) in ("Mn", "Me", "Cf"):
            return 0
        return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1

    lines = []
    for line in message.splitlines()[:rows]:
        size, end = 0, 0
        line = line.expandtabs(4)
        for char in line:
            cells = width(char)
            if size + cells > columns:
                break
            size += cells
            end += 1
        lines.append((line[:end], size))
    top = (rows - len(lines)) // 2 + 1
    left = (columns - max((size for _, size in lines), default=0)) // 2 + 1
    return "\x1b[?25l" + "".join(f"\x1b[{top + i};{left}H{line}" for i, (line, _) in enumerate(lines))


class AttachPreview:
    def __init__(self, name, host):
        from . import kitty_instance

        self.shared = kitty_instance.context()
        self.nonce = uuid.uuid4().hex
        self.window = int(kitty_instance.remote_raw(
            self.shared["address"], "launch", "--type=os-window", "--tab-title=Connecting…",
            f"--var=kzw_attach_preview={self.nonce}", sys.executable, "-I", str(Path(__file__).resolve()),
            splash_message(name, host)).strip())
        self.populated = False

    def command(self, action, value=""):
        from . import kitty_instance

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


if __name__ == "__main__":
    try:
        columns, rows = os.get_terminal_size()
    except OSError:
        columns, rows = 80, 24
    sys.stdout.write(splash_screen(sys.argv[1], columns or 80, rows or 24))
    sys.stdout.flush()
    os.execl("/bin/sleep", "sleep", "86400")
