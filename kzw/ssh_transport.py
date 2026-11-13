"""Best-effort SSH transport sharing for panes of one workspace and host."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


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


def prepare_attachment(command, env):
    """Start a command-free master before requesting a pane's terminal channel.

    Never let the interactive SSH process fork into a ControlPersist master:
    that requires reconnecting to itself and can fail during concurrent attach.
    A failed preflight uses a direct connection, without deleting sockets or
    replaying the terminal command. Existing master checks are local only.
    """
    if (len(command) != 10 or command[0] != "ssh" or command[1:6] !=
            ["-o", "ControlMaster=auto", "-o", "ControlPersist=60", "-o"] or
            not command[6].startswith("ControlPath=") or command[7] != "-t"):
        return command
    path = Path(command[6].split("=", 1)[1])
    host = command[8]
    direct = ["ssh", "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ControlPath=none", *command[7:]]
    shared = [*direct[:6], "ControlPath=" + str(path), *command[7:]]

    def run(*args, timeout=3):
        return subprocess.run(["ssh", *args], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout, check=False)

    def alive():
        return run("-S", str(path), "-O", "check", "-o", "BatchMode=yes", host).returncode == 0

    # Only serialize setup, never the lifetime of the terminal attachment.
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe SSH master setup lock")
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return direct
                time.sleep(0.05)
        try:
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    return direct
                return shared if alive() else direct
            # Adding -M after ControlMaster=yes toggles OpenSSH into ask mode.
            result = run("-o", "ControlMaster=yes", "-o", "ControlPersist=60", "-o", "ControlPath=" + str(path),
                         "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
                         "-N", "-f", "-T", host, timeout=15)
            return shared if result.returncode == 0 and alive() else direct
        except (OSError, subprocess.SubprocessError) as exc:
            if env.get("KZW_DEBUG") == "1":
                print("kzw: SSH sharing unavailable: " + str(exc), file=sys.stderr, flush=True)
            return direct
    finally:
        os.close(fd)
