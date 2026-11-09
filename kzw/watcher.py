"""Kitty watcher: notify the supervisor about focus changes without blocking it."""

import os


def on_focus_change(boss, window, data):
    path = os.environ.get("KZW_FOCUS_SIGNAL")
    if path:
        try:
            with open(path, "a", encoding="utf-8"):
                os.utime(path, None)
        except OSError:
            pass
