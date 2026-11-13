"""Read-only Linux probes, sent over SSH by demo.py; never installed remotely."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys


class ProbeError(ValueError):
    def __init__(self, code, programs=()):
        self.code = code
        self.programs = list(programs)
        super().__init__(code)


def find_program(name):
    found = shutil.which(name)
    if found:
        return found
    # setup.sh installs Yazi (and other Rust tools) here. Noninteractive SSH
    # need not run the .bashrc stanza that adds this directory to PATH.
    candidate = Path.home() / ".cargo/bin" / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def preflight():
    if sys.platform != "linux" or sys.version_info < (3, 8):
        raise ProbeError("unsupported-origin")
    tools = {}
    required = ("python3", "bash", "env", "date", "sleep", "zmx", "htop", "yazi")
    for name in (*required, "cmatrix"):
        tools[name] = find_program(name)
    missing = [name for name in required if not tools[name]]
    if missing:
        raise ProbeError("missing-programs", missing)
    target = Path.home() / "dotfiles/.config/nvim"
    if not target.is_dir():
        raise ProbeError("missing-directory")
    versions = {}
    for name in ("zmx", "cmatrix", "htop", "yazi"):
        if not tools[name]:
            continue
        flag = "-V" if name == "cmatrix" else "--version"
        result = subprocess.run([tools[name], flag], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise ProbeError("version-check", [name])
        versions[name] = (result.stdout + result.stderr).strip()[:1000]
    return {"tools": tools, "versions": versions, "hostname": socket.gethostname(),
            "uid": os.getuid(), "python": sys.version, "target": str(target)}


def start_tick(path):
    # comm (field 2) can itself contain spaces or parentheses.
    return (path / "stat").read_text().rsplit(")", 1)[1].split()[19]


def processes(token, expected, proc=Path("/proc")):
    if not token or not expected:
        raise ValueError("missing process-selection fence")
    found = {}
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            tick = start_tick(path)
            env = dict(item.split(b"=", 1) for item in (path / "environ").read_bytes().split(b"\0")
                       if b"=" in item)
            if env.get(b"KZW_DEMO_TOKEN") != token.encode():
                continue
            role = env.get(b"KZW_DEMO_ROLE", b"").decode()
            command = (path / "cmdline").read_bytes().split(b"\0")[0].decode()
            if role not in expected or Path(command).name != expected[role]:
                continue
            if start_tick(path) != tick:
                continue
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if role in found:
            raise ValueError("ambiguous demo process: " + role)
        found[role] = {"pid": int(path.name), "start_tick": tick}
    return {"boot_id": (proc / "sys/kernel/random/boot_id").read_text().strip(), "processes": found}


def main():
    request = json.loads(sys.argv[1])
    try:
        if request["mode"] == "preflight":
            result = preflight()
        elif request["mode"] == "processes":
            result = processes(request["token"], request["expected"])
        else:
            raise ValueError("unknown probe")
    except ProbeError as error:
        result = {"error": {"code": error.code, "programs": error.programs}}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
