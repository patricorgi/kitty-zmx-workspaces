"""Host-local, terminal-only selection of coordinator-owned zmx sessions."""

import json
import os
from pathlib import Path
import select
import shlex
import socket
import stat
import subprocess
import sys
import termios
import tty

from .core import PANE, check_name
from . import session_meta
from .remote_client import RemoteRejected
from .titles import read_process_details

MAX_REPLY = 1024 * 1024


def coordinator_request(request):
    """Send a request to the host's existing remote-origin coordinator."""
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kzw"
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe coordinator state directory")
    path = root / "server.sock"
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe coordinator socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall((json.dumps(request) + "\n").encode())
        with client.makefile("rb") as stream:
            data = stream.readline(MAX_REPLY + 1)
    if not data.endswith(b"\n") or len(data) > MAX_REPLY:
        raise ValueError("invalid coordinator response")
    response = json.loads(data)
    if not isinstance(response, dict):
        raise ValueError("invalid coordinator response")
    if not response.get("ok"):
        raise RemoteRejected("coordinator: " + str(response.get("error", "unknown error")), response.get("code"))
    if not isinstance(response.get("result"), dict):
        raise ValueError("invalid coordinator response")
    return response["result"]


def coordinator_snapshot(name):
    """Query the host's existing remote-origin coordinator without a lease."""
    check_name(name)
    result = coordinator_request({"method": "workspace.sync", "workspace": name})
    if not result.get("exists"):
        raise ValueError("workspace does not exist on this host")
    return result


def snapshot_tabs(snapshot, manifest):
    """Extract ordered panes from a validated portable Kitty session."""
    if not isinstance(snapshot, str) or not isinstance(manifest, list):
        raise ValueError("invalid coordinator snapshot")
    if any(not isinstance(p, str) or not PANE.fullmatch(p) for p in manifest) or len(set(manifest)) != len(manifest):
        raise ValueError("invalid workspace pane manifest")
    if snapshot in ("# kzw-dormant-workspace\n", "# kzw-empty-workspace\n"):
        if manifest:
            raise ValueError("empty layout conflicts with pane manifest")
        return []
    tabs = []
    for line in snapshot.splitlines():
        if line.startswith("new_tab ") or line == "new_tab":
            tabs.append([])
        elif line.startswith("launch "):
            args = shlex.split(line)
            if not tabs or len(args) < 8 or not args[1].startswith("kitty-unserialize-data="):
                raise ValueError("invalid portable Kitty layout")
            command = args[-6:]
            if (command[0] != "KZW_PANE_EXECUTABLE" or command[1] != "--pane" or
                    not PANE.fullmatch(command[2]) or command[3:] != ["--host", "KZW_ORIGIN", "--existing"]):
                raise ValueError("layout contains an unmanaged pane")
            tabs[-1].append(command[2])
    panes = [pane for tab in tabs for pane in tab]
    if any(not tab for tab in tabs) or len(panes) != len(set(panes)) or set(panes) != set(manifest):
        raise ValueError("layout and pane manifest disagree")
    return tabs


def live_sessions():
    env = os.environ.copy()
    env.pop("ZMX_SESSION", None)
    env.pop("ZMX_SESSION_PREFIX", None)
    env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
    result = subprocess.run(["zmx", "list", "--short"], env=env, text=True,
                            capture_output=True, timeout=10, check=True)
    return set(result.stdout.splitlines())


def process_labels():
    try:
        processes = read_process_details("")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    labels = {}
    for pane, detail in processes.items():
        name = detail["name"]
        if name == "bash":
            command = session_meta.read(pane, detail["pid"])
            labels[pane] = "bash: " + (command or "unavailable")
        else:
            labels[pane] = name
    return labels


def choose(name, tabs, live, input_stream=sys.stdin, output=sys.stdout, labels=None):
    if not input_stream.isatty() or not output.isatty():
        raise ValueError("session selection requires an interactive terminal")
    panes = [pane for tab in tabs for pane in tab if pane in live]
    if not panes:
        raise ValueError("workspace has no available sessions")
    fd = input_stream.fileno()
    saved = termios.tcgetattr(fd)
    selected = 0
    labels = labels or {}
    shortcuts = {pane: str(index) for index, pane in enumerate(panes[:10])}
    try:
        tty.setcbreak(fd)
        output.write("\x1b[?1049h\x1b[?25l")
        while True:
            lines = [name]
            for tab_number, tab in enumerate(tabs, 1):
                lines.append(f"  Tab {tab_number}")
                for split_number, pane in enumerate(tab, 1):
                    marker = ">" if pane == panes[selected] else " "
                    status = "" if pane in live else " [unavailable]"
                    process = labels.get(pane, "unknown")
                    shortcut = shortcuts.get(pane, " ")
                    lines.append(f"  {marker} [{shortcut}] Split {split_number} — {process}{status}")
            output.write("\x1b[H\x1b[2J" + "\r\n".join(lines) + "\r\n\r\n0–9: attach  ↑/↓: select  Enter: attach  q/Esc: cancel\r\n")
            output.flush()
            key = os.read(fd, 1)
            if key == b"\x1b":
                for _ in range(2):
                    if not select.select([fd], [], [], 0.15)[0]:
                        break
                    key += os.read(fd, 1)
                if key == b"\x1b":
                    return None
            if key in (b"\x1b[A", b"k"):
                selected = (selected - 1) % len(panes)
            elif key in (b"\x1b[B", b"j"):
                selected = (selected + 1) % len(panes)
            elif key in (b"\r", b"\n"):
                return panes[selected]
            elif len(key) == 1 and b"0" <= key <= b"9":
                index = key[0] - ord("0")
                if index < min(10, len(panes)):
                    return panes[index]
            elif key in (b"q", b"\x03", b""):
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        output.write("\x1b[?25h\x1b[?1049l")
        output.flush()


def list_and_attach(name):
    initial = coordinator_snapshot(name)
    tabs = snapshot_tabs(initial.get("snapshot"), initial.get("panes"))
    pane = choose(name, tabs, live_sessions(), labels=process_labels())
    if pane is None:
        return 0
    latest = coordinator_snapshot(name)
    if latest.get("revision") != initial.get("revision") or pane not in latest.get("panes", []):
        raise ValueError("workspace changed during selection; try again")
    if pane not in live_sessions():
        raise ValueError("selected session is no longer available")
    env = os.environ.copy()
    env.pop("ZMX_SESSION", None)
    env.pop("ZMX_SESSION_PREFIX", None)
    env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
    # zmx 0.5 has no attach-only mode. If the session vanishes between the
    # final check and attach, do not start a replacement interactive shell.
    os.execvpe("zmx", ["zmx", "attach", pane, "/bin/false"], env)
