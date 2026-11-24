#!/usr/bin/env python3
"""Install a standalone kzw executable. Kitty integration is opt-in."""

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def install(bindir, root):
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / "kzw"
    if os.path.lexists(target):
        raise FileExistsError(f"refusing to overwrite {target}")
    files = {f"kzw/{p.name}": p.read_bytes() for p in sorted((root / "kzw").iterdir())
             if p.suffix in (".py", ".sh")}
    boot = (root / "scripts/bundle_main.py").read_bytes()
    digest = hashlib.sha256(boot)
    for name, content in files.items():
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    files["__main__.py"] = boot.replace(b"__BUILD_ID__", digest.hexdigest().encode())
    fd, temporary = tempfile.mkstemp(dir=bindir)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
        os.chmod(temporary, 0o755)
        os.link(temporary, target)  # atomic, never clobbers an existing file
    finally:
        os.unlink(temporary)
    return target


def install_kitty_hook(config, root):
    """Kitty has one launch wrapper slot. Never replace an existing hook."""
    path = Path(config) / "launch_wrapper.py"
    expected = (root / "kzw" / "native_tab.py").resolve()
    if not (path.is_symlink() and path.resolve() == expected):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(expected)
    conf = path.with_name("kitty.conf")
    watcher = f"watcher {root / 'kzw/native_close.py'}"
    if not conf.exists() or watcher not in conf.read_text().splitlines():
        with conf.open("a", encoding="utf-8") as file:
            file.write(f"\n# kzw native pane-close integration\n{watcher}\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin",
                        help="directory for the kzw executable (default: ~/.local/bin)")
    parser.add_argument("--kitty-hook-only", action="store_true", help="add the native tab hook to an existing installation")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if not args.kitty_hook_only:
        try:
            target = install(args.bin_dir.expanduser().resolve(), root)
        except OSError as exc:
            print(f"kzw install: {exc}", file=sys.stderr)
            return 1
        print(f"Installed {target}")
        return 0
    config = os.environ.get("KITTY_CONFIG_DIRECTORY") or str(
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kitty")
    try:
        program = args.bin_dir.expanduser().resolve() / "kzw"
        with zipfile.ZipFile(program) as archive:
            if "__main__.py" not in archive.namelist():
                raise ValueError(f"not a kzw bundle: {program}")
        runtime = Path(subprocess.check_output([str(program), "_runtime-directory"], text=True).strip())
        path = install_kitty_hook(config, runtime)
        print(f"Native tab and pane-close hooks installed in {path.parent} (Kitty 0.51+; reload Kitty config).")
        return 0
    except FileExistsError:
        print(f"kzw install: leaving {Path(config) / 'launch_wrapper.py'} unchanged; "
              "native tab integration was not installed", file=sys.stderr)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        print(f"kzw install: native tab integration was not installed: {exc}", file=sys.stderr)
    return int(args.kitty_hook_only)


if __name__ == "__main__":
    raise SystemExit(main())
