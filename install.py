#!/usr/bin/env python3
"""Install checkout-backed kzw launchers without replacing existing files."""

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


def main():
    try:
        root = Path(__file__).resolve().parent
        bindir = install(Path.home(), root)
    except OSError as exc:
        print(f"kzw install: {exc}", file=sys.stderr)
        return 1
    print(f"Installed kzw and kzw-pane to {bindir}")
    print(f"These launchers use this checkout: {root}")
    print(f"Ensure {bindir} is on PATH and keep the checkout in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
