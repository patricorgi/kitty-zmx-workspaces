"""Attach kzw workspaces to the Kitty instance that invoked the command.

Kitty's single-instance launcher owns an instance-wide UNIX socket named
``kitty-ipc-<euid>`` or ``kitty-ipc-<euid>-<group>``.  A second Kitty process
started with ``--single-instance --instance-group <group>`` forwards its
session to the original process and exits.  That is the only supported way to
add a full workspace OS window to an already running Kitty without taking over
any of its other windows.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

SOCKET_PREFIX = "kitty-ipc-"
HOST = re.compile(r"^[A-Za-z0-9_.@:-]+$")


def address(value):
    """Return a Kitty remote-control address from a path or full address."""
    text = str(value)
    return text if text.startswith(("unix:", "tcp:")) else "unix:" + text


def remote_raw(value, *command, timeout=5):
    return subprocess.run(["kitty", "@", "--to", address(value), *command],
                          check=True, capture_output=True, text=True, timeout=timeout).stdout


def os_windows(value):
    """Return the raw Kitty OS-window tree, never filtered by kzw metadata."""
    tree = json.loads(remote_raw(value, "ls"))
    if not isinstance(tree, list) or any(not isinstance(item, dict) or type(item.get("id")) is not int for item in tree):
        raise ValueError("invalid Kitty window listing")
    return tree


def group_from_socket(path, euid):
    name = Path(path).name
    suffix = SOCKET_PREFIX + str(euid)
    if name == suffix:
        return ""
    prefix = suffix + "-"
    if name.startswith(prefix):
        group = name[len(prefix):]
        if re.fullmatch(r"[A-Za-z0-9_.@:-]+", group):
            return group
    return None


def single_instance_group(pid, euid=None):
    """Find the single-instance group owned by an already running Kitty."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("invalid Kitty process ID")
    euid = os.geteuid() if euid is None else euid
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-U", "-Fn"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if not line.startswith("n/"):
            continue
        group = group_from_socket(line[1:], euid)
        if group is not None:
            return group
    return None


def context(env=None):
    """Validate that this process can add an OS window to its Kitty instance."""
    env = os.environ if env is None else env
    raw_pid = env.get("KITTY_PID", "")
    listen = env.get("KITTY_LISTEN_ON", "")
    if not re.fullmatch(r"[1-9][0-9]*", str(raw_pid)):
        raise ValueError("not running inside Kitty; use --new-kitty-instance")
    if not str(listen).startswith(("unix:", "tcp:")):
        raise ValueError("Kitty remote control is unavailable; enable allow_remote_control and listen_on, or use --new-kitty-instance")
    pid = int(raw_pid)
    group = single_instance_group(pid)
    if group is None:
        raise ValueError("the current Kitty instance has no single-instance socket; "
                         "start Kitty with --single-instance or use --new-kitty-instance")
    return {"pid": pid, "address": str(listen), "group": group}


def probe(value):
    """Fail closed with an actionable message when remote control is disabled."""
    try:
        os_windows(value)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("cannot control the current Kitty instance; "
                         "enable allow_remote_control and listen_on, or use --new-kitty-instance") from exc


def launch_command(group, session):
    command = ["kitty", "--single-instance"]
    if group:
        command += ["--instance-group", group]
    return command + ["--session", str(session)]


def tab_style_state(address, pid):
    """One saved original-tab-style record per Kitty instance."""
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kzw" / "tab-style"
    key = hashlib.sha256((str(address) + "\0" + str(pid)).encode()).hexdigest()[:32]
    return root / (key + ".json")


def tab_style(address, pid, action):
    """Apply or restore kzw's instance-wide tab template inside Kitty."""
    if action not in ("apply", "restore"):
        raise ValueError("invalid tab-style action")
    kitten = Path(__file__).with_name("tab_style_kitten.py")
    return remote_raw(address, "kitten", str(kitten), action, str(tab_style_state(address, pid)))

