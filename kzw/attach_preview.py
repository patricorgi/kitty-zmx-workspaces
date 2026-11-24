"""A disposable local Kitty window while the remote attach is validated."""

import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
import uuid


def splash_directory():
    return (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kzw").absolute()


def read_template(path, limit):
    try:
        if not path.is_file():
            return ""
        with path.open(encoding="utf-8") as stream:
            template = stream.read(limit + 1)
    except (OSError, UnicodeError):
        return ""
    return template if len(template) <= limit else ""


def expand_template(template, name, host):
    values = {"workspace": name, "host": host}
    text = re.sub(r"\{(workspace|host)\}", lambda m: values[m[1]], template)
    # Allow line breaks and indentation, but never pass through terminal controls.
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")


def splash_message(name, host, directory=None):
    template = read_template((directory or splash_directory()) / "splash.txt", 8192)
    text = expand_template(template, name, host)
    return text if text.strip() else expand_template("Connecting to {workspace} ({host})...", name, host)


def splash_frames(directory, name, host):
    source = read_template(directory / "splash.frames", 262144)
    frames = []
    lines = []
    delay = None
    for line in source.removesuffix("\n").split("\n"):
        if line.startswith("@frame"):
            header = re.fullmatch(r"@frame ([0-9]{1,5})", line)
            if not header or not 50 <= int(header[1]) <= 10000:
                return []
            if delay is not None:
                frames.append((delay, "\n".join(lines)))
            delay = int(header[1])
            lines = []
        elif delay is None:
            return []
        else:
            lines.append(line)
    if delay is not None:
        frames.append((delay, "\n".join(lines)))
    if not 1 <= len(frames) <= 256 or any(len(text) > 8192 for _, text in frames):
        return []
    frames = [(ms, expand_template(text, name, host)) for ms, text in frames]
    return frames if all(text.strip() for _, text in frames) else []


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
            str(splash_directory()), name, host).strip())
        self.populated = False

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, traceback):
        if not self.populated:
            if isinstance(exc, Exception):
                self.error(exc)
            else:
                self.close()

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


def render_frame(message, redraw=False):
    try:
        columns, rows = os.get_terminal_size(sys.stdout.fileno())
    except OSError:
        columns, rows = 80, 24
    screen = splash_screen(message, columns or 80, rows or 24)
    if redraw:
        # Present the erase and repaint together; shorter frames leave no stale cells.
        screen = "\x1b[?2026h\x1b[2J" + screen + "\x1b[?2026l"
    sys.stdout.write(screen)
    sys.stdout.flush()


def show_preview(directory, name, host):
    frames = splash_frames(directory, name, host)
    if len(frames) > 1:
        sys.stdout.write("\x1b[?1049h")  # Keep repeated frames out of scrollback.
        while True:
            for ms, text in frames:
                render_frame(text, redraw=True)
                time.sleep(ms / 1000)
    render_frame(frames[0][1] if frames else splash_message(name, host, directory))
    os.execl("/bin/sleep", "sleep", "86400")


if __name__ == "__main__":
    show_preview(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
