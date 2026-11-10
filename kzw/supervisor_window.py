"""Best-effort reversible macOS supervisor window minimization."""

import os
from pathlib import Path
import re
import subprocess
import sys


def source_window():
    window = os.environ.get("KITTY_WINDOW_ID", "")
    if (sys.platform != "darwin" or os.environ.get("KZW_TEST_KITTY_BACKGROUND") == "1" or
            not sys.stdin.isatty() or not re.fullmatch(r"[1-9][0-9]*", window)):
        return None
    return window


def change(window, action):
    if not re.fullmatch(r"[1-9][0-9]*", window) or action not in ("minimize", "restore"):
        raise ValueError("invalid supervisor window operation")
    kitten = Path(__file__).with_name("supervisor_window_kitten.py")
    result = subprocess.run(["kitty", "@", "kitten", "--match", "id:" + window, str(kitten), action],
                            capture_output=True, text=True, check=True, timeout=15)
    if result.stdout.strip() != "ok":
        raise ValueError("Kitty did not confirm supervisor window operation")
