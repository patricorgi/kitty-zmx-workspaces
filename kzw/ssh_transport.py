"""Best-effort SSH transport sharing for panes of one workspace and host."""

import hashlib
import json
import os
from pathlib import Path
import stat


def pane_options(host, env):
    """Return SSH multiplexing options, without changing SSH configuration.

    OpenSSH falls back to an independent connection when a master has reached
    the server's session limit. The short-lived master is never explicitly
    killed by kzw, since it may still carry other pane sessions.
    """
    workspace = env.get("KZW_WORKSPACE", "")
    if not host or not workspace:
        return []
    root = Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kzw" / "ssh"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe kzw SSH control socket directory")
    identity = json.dumps([workspace, env.get("KZW_WORKSPACE_ORIGIN", ""), host], separators=(",", ":"))
    socket_path = root / hashlib.sha256(identity.encode()).hexdigest()[:20]
    # AF_UNIX path limits differ between platforms; fail rather than silently
    # truncating into another workspace's socket.
    if len(os.fsencode(str(socket_path))) >= 100:
        raise ValueError("kzw SSH control socket path is too long")
    return ["-o", "ControlMaster=auto", "-o", "ControlPersist=60",
            "-o", "ControlPath=" + str(socket_path)]
