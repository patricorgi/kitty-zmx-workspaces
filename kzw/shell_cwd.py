"""Validate shell-reported working directories before using them on an origin."""

import re
import socket
import subprocess
from urllib.parse import unquote, urlsplit


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
