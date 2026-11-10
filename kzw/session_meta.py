"""Private, opt-in per-session Bash command metadata."""

import json
import os
from pathlib import Path
import stat

from .core import PANE, atomic_write


def metadata_dir(create=False):
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kzw" / "session-meta"
    if create:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe session metadata directory")
    return root


def clean_command(command):
    return " ".join("".join(c if c.isprintable() else " " for c in command).split())[:160]


def record(pane, pid, command):
    if not PANE.fullmatch(pane) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("invalid Bash session identity")
    command = clean_command(command)
    if not command:
        return
    root = metadata_dir(create=True)
    atomic_write(root / (pane + ".json"), json.dumps({"pid": pid, "command": command}) + "\n")


def read(pane, pid):
    if not PANE.fullmatch(pane) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        path = metadata_dir() / (pane + ".json")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            raw = stream.read(8193)
        if len(raw) > 8192:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("pid") != pid or not isinstance(value.get("command"), str):
            return None
        return clean_command(value["command"]) or None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
