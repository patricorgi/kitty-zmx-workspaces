"""Read origin-shell working directories, with validated reports as a fallback."""

import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from .core import PANE


def from_session(pane, host):
    """Read the selected persistent shell, never the local SSH wrapper's cwd."""
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        raise ValueError("invalid source session")
    if host and (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host)):
        raise ValueError("invalid origin")
    probe = Path(__file__).with_name("process_cwd.py")
    if host:
        script = ('PATH="$HOME/.local/bin:$PATH"; export PATH; unset ZMX_SESSION ZMX_SESSION_PREFIX; ' +
                  shlex.join(["python3", "-c", probe.read_text(), pane]))
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, script]
    else:
        command = [sys.executable, str(probe), pane]
    env = os.environ.copy()
    env.pop("ZMX_SESSION", None)
    env.pop("ZMX_SESSION_PREFIX", None)
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=15)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        raise ValueError(detail[-1][-500:] if detail else "source process cwd lookup failed") from None
    except subprocess.TimeoutExpired:
        raise ValueError("source process cwd lookup timed out") from None
    data = json.loads(result.stdout)
    if (not isinstance(data, dict) or data.get("pane") != pane or type(data.get("pid")) is not int
            or data["pid"] <= 0 or not isinstance(data.get("created"), str) or not data["created"]):
        raise ValueError("source session cwd response has an invalid identity")
    cwd = data.get("cwd")
    if (not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > 8192
            or any(ord(c) < 32 or ord(c) == 127 for c in cwd)):
        raise ValueError("source session cwd response is invalid")
    return cwd


def resolve(uri, host):
    if not isinstance(uri, str) or len(uri) > 8192:
        raise ValueError("invalid shell working-directory report")
    if uri.startswith('kitty-shell-cwd://'):
        match = re.fullmatch(r'kitty-shell-cwd://([^/]+)(/.*)', uri)
        if not match or not re.fullmatch(r'[A-Za-z0-9_.-]+', match.group(1)):
            raise ValueError("unsupported shell working-directory report")
        hostname, path = match.groups()
    else:
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or not parsed.hostname or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            raise ValueError("unsupported shell working-directory report")
        hostname, path = parsed.hostname, unquote(parsed.path, errors="strict")
    if not path.startswith('/') or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError("invalid shell working directory")
    if host:
        if host.startswith('-'):
            raise ValueError("invalid origin")
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', host,
                                 'hostname; hostname -f 2>/dev/null || :'], capture_output=True, text=True, timeout=10, check=True)
        names = {name.rstrip('.').lower() for name in result.stdout.splitlines() if name}
    else:
        names = {socket.gethostname().rstrip('.').lower(), socket.getfqdn().rstrip('.').lower(), 'localhost'}
    if hostname.rstrip('.').lower() not in names:
        raise ValueError("shell working directory belongs to another host")
    return path
