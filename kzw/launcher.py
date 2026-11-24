"""Locate the public executable in the CLI or a managed Kitty child."""

import os
import shutil


def executable(window=None):
    env = os.environ
    if window is not None:
        env = getattr(getattr(window, "child", None), "final_env", {})
    path = env.get("KZW_EXECUTABLE") if hasattr(env, "get") else None
    if isinstance(path, str) and os.path.isabs(path):
        return path
    path = shutil.which("kzw")
    if path:
        return os.path.abspath(path)
    if window is not None:
        return os.path.expanduser("~/.local/bin/kzw")
    raise ValueError("cannot locate kzw; run the installed executable by its full path")
