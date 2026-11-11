"""Read a zmx session's shell cwd; standalone for execution on an SSH origin."""

import json
import os
import re
import subprocess
import sys


def session_identity(pane):
    listing = subprocess.run(["zmx", "list"], capture_output=True, text=True, check=True, timeout=5).stdout
    matches = []
    for line in listing.splitlines():
        fields = dict(field.split("=", 1) for field in line.lstrip().split("\t") if "=" in field)
        if fields.get("name") == pane:
            matches.append(fields)
    if len(matches) != 1:
        raise ValueError("source session is missing or ambiguous")
    fields = matches[0]
    if not fields.get("pid", "").isdigit() or int(fields["pid"]) <= 0 or not fields.get("created"):
        raise ValueError("source session identity is incomplete")
    return int(fields["pid"]), fields["created"]


def process_identity(pid):
    if sys.platform.startswith("linux"):
        root = "/proc/" + str(pid)
        if os.stat(root).st_uid != os.getuid():
            raise ValueError("source process belongs to another user")
        with open(root + "/stat") as stream:
            return stream.read().rsplit(")", 1)[1].split()[19]
    if sys.platform == "darwin":
        fields = subprocess.run(["ps", "-p", str(pid), "-o", "uid=,lstart="], capture_output=True,
                                text=True, check=True, timeout=5).stdout.split()
        if len(fields) != 6 or fields[0] != str(os.getuid()):
            raise ValueError("source process identity is unavailable")
        return " ".join(fields[1:])
    raise ValueError("process cwd lookup is unsupported on this platform")


def process_cwd(pid):
    if sys.platform.startswith("linux"):
        return os.readlink("/proc/" + str(pid) + "/cwd")
    output = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn0"],
                            capture_output=True, text=True, check=True, timeout=5).stdout
    paths = [field[1:] for field in output.split("\0") if field.startswith("n")]
    if len(paths) != 1:
        raise ValueError("source working directory is unavailable")
    return paths[0]


def read(pane):
    if not isinstance(pane, str) or not re.fullmatch(r"kzw-[0-9a-f]{32}", pane):
        raise ValueError("invalid source session")
    pid, created = session_identity(pane)
    identity = process_identity(pid)
    cwd = process_cwd(pid)
    if (not cwd.startswith("/") or len(cwd) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in cwd)
            or not os.path.isdir(cwd)):
        raise ValueError("source working directory is invalid or unavailable")
    if process_identity(pid) != identity or session_identity(pane) != (pid, created):
        raise ValueError("source session changed during cwd lookup")
    return {"pane": pane, "pid": pid, "created": created, "cwd": cwd}


if __name__ == "__main__":
    try:
        print(json.dumps(read(sys.argv[1])))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        print("kzw: cannot read source session cwd: " + str(exc), file=sys.stderr)
        sys.exit(1)
