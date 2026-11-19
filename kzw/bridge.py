"""Build and install the same SSH bridge that kzw otherwise ships inline."""

import argparse
import base64
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import zlib

from .core import atomic_write


def check_host(host):
    if not isinstance(host, str) or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host):
        raise ValueError("invalid SSH host")


def encoded_source():
    return base64.b64encode(Path(__file__).with_name("remote.py").read_bytes())


def host_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kzw" / "installed-bridges"


def installed_hosts():
    try:
        return set(host_file().read_text().split())
    except FileNotFoundError:
        return set()


def executable(source):
    payload = base64.b64encode(zlib.compress(base64.b64decode(source)))
    return (
        "#!/usr/bin/env python3\n"
        "import base64, hashlib, os, sys, zlib\n"
        f"source = zlib.decompress(base64.b64decode({payload!r}))\n"
        "encoded = base64.b64encode(source)\n"
        "build = hashlib.sha256(encoded).hexdigest()\n"
        "if sys.argv[1:] == ['--build']:\n"
        "    print(build)\n"
        "    sys.exit(0)\n"
        "if sys.argv[1:] != [build]:\n"
        "    sys.exit('kzw: installed bridge version differs; reinstall it from your local checkout')\n"
        "os.environ.pop('KZW_LOCAL_COORDINATOR', None)\n"
        "os.environ['KZW_COORDINATOR_SOURCE'] = encoded.decode()\n"
        "del sys.argv[1:]\n"
        "exec(compile(source, '<kzw bridge>', 'exec'))\n"
    ).encode()


def install(host, enable=True, *, stage=False):
    check_host(host)
    source = encoded_source()
    expected = hashlib.sha256(source).hexdigest()
    target = "kzw-bridge-" + expected if stage else "kzw-bridge"
    # Stage and validate before replacing the launcher. Existing processes have
    # their own copy of the source and are not restarted or signalled.
    script = (
        'set -eu; umask 077; dir="$HOME/.local/bin"; mkdir -p "$dir"; '
        'stage=$(mktemp "$dir/.kzw-bridge.XXXXXXXX"); trap \'rm -f -- "$stage"\' EXIT; '
        'cat > "$stage"; chmod 700 "$stage"; '
        f'test "$("$stage" --build)" = {expected}; '
        f'test ! -d "$dir/{target}"; mv -f -- "$stage" "$dir/{target}"'
    )
    subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, script],
                   input=executable(source), check=True, timeout=30)
    if enable:
        atomic_write(host_file(), "\n".join(sorted(installed_hosts() | {host})) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--no-enable", action="store_true", help="install without enabling this SSH host locally")
    parser.add_argument("--stage", action="store_true", help="install alongside the current bridge for a rolling upgrade")
    args = parser.parse_args()
    try:
        install(args.host, enable=not args.no_enable, stage=args.stage)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"kzw bridge: {exc}", file=sys.stderr)
        return 1
    print(f"Installed {'versioned ' if args.stage else ''}kzw-bridge in ~/.local/bin on {args.host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
