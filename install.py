#!/usr/bin/env python3
"""Install checkout-backed launchers and Kitty integration without overwriting files."""

import argparse
import os
from pathlib import Path
import shlex
import sys


def install(home, root):
    bindir = Path(home) / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    targets = [(bindir / "kzw", "kzw.cli"), (bindir / "kzw-pane", "kzw.pane")]
    for path, _ in targets:
        if os.path.lexists(path):
            raise FileExistsError(f"refusing to overwrite {path}")
    made = []
    try:
        for path, module in targets:
            script = ("#!/bin/sh\nset -eu\n"
                      f"PATH={shlex.quote(str(bindir))}:\"$PATH\"\n"
                      f"PYTHONPATH={shlex.quote(str(root))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
                      "export PATH PYTHONPATH\n"
                      f"exec python3 -m {module} \"$@\"\n")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
            made.append(path)
            with os.fdopen(fd, "w") as file:
                file.write(script)
            path.chmod(0o755)
    except Exception:
        for path in made:
            path.unlink(missing_ok=True)
        raise
    return bindir


def install_kitty_hook(config, root):
    """Kitty has one launch wrapper slot. Never replace an existing hook."""
    path = Path(config) / "launch_wrapper.py"
    expected = root / "kzw" / "native_tab.py"
    if path.is_symlink() and path.resolve() == expected:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(expected)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kitty-hook-only", action="store_true", help="add the native tab hook to an existing installation")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if not args.kitty_hook_only:
        try:
            bindir = install(Path.home(), root)
        except OSError as exc:
            print(f"kzw install: {exc}", file=sys.stderr)
            return 1
        print(f"Installed kzw and kzw-pane to {bindir}")
        print(f"These launchers use this checkout: {root}")
        print(f"Ensure {bindir} is on PATH and keep the checkout in place.")
    config = os.environ.get("KITTY_CONFIG_DIRECTORY") or str(
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kitty")
    try:
        path = install_kitty_hook(config, root)
        print(f"Native tab hook installed at {path} (Kitty 0.51+; reload Kitty config if already running).")
        return 0
    except FileExistsError:
        print(f"kzw install: leaving {Path(config) / 'launch_wrapper.py'} unchanged; "
              "native tab integration was not installed", file=sys.stderr)
    except OSError as exc:
        print(f"kzw install: native tab integration was not installed: {exc}", file=sys.stderr)
    return int(args.kitty_hook_only)


if __name__ == "__main__":
    raise SystemExit(main())
