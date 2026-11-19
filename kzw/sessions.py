"""Terminal-only selection of coordinator-owned zmx sessions."""

import json
import os
from pathlib import Path
import re
import select
import shlex
import socket
import stat
import subprocess
import sys
import termios
import tty

from .core import PANE, check_name
from . import remote_client, session_meta
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


def choose(name, tabs, live, input_stream=sys.stdin, output=sys.stdout, labels=None, preview=None,
           previous=None, notice=""):
    if not input_stream.isatty() or not output.isatty():
        raise ValueError("session selection requires an interactive terminal")
    panes = [pane for tab in tabs for pane in tab if pane in live]
    fd = input_stream.fileno()
    saved = termios.tcgetattr(fd)
    selected = panes.index(previous) if previous in panes else 0
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
                    marker = ">" if panes and pane == panes[selected] else " "
                    status = "" if pane in live else " [unavailable]"
                    process = labels.get(pane, "unknown")
                    shortcut = shortcuts.get(pane, " ")
                    lines.append(f"  {marker} [{shortcut}] Split {split_number} — {process}{status}")
            if notice:
                lines.append(notice)
            if not panes:
                lines.append("No available sessions.")
            help_text = ("0–9: attach  ↑/↓: select  Enter: attach  q/Esc: quit"
                         if panes else "q/Esc: quit")
            if preview and panes:
                help_text += "  p: preview"
            output.write("\x1b[H\x1b[2J" + "\r\n".join(lines) + "\r\n\r\n" + help_text + "\r\n")
            output.flush()
            key = os.read(fd, 1)
            if key == b"\x1b":
                for _ in range(2):
                    if not select.select([fd], [], [], 0.15)[0]:
                        break
                    key += os.read(fd, 1)
                if key == b"\x1b":
                    return None
            if key in (b"q", b"\x03", b""):
                return None
            if not panes:
                continue
            if key in (b"\x1b[A", b"k"):
                selected = (selected - 1) % len(panes)
            elif key in (b"\x1b[B", b"j"):
                selected = (selected + 1) % len(panes)
            elif key == b"p" and preview:
                try:
                    screen = preview(panes[selected])
                except (OSError, ValueError, subprocess.SubprocessError):
                    screen = "Preview unavailable."
                output.write("\x1b[H\x1b[2J" + "\r\n".join(display_text(row, 200) for row in
                             screen.splitlines()[-20:]) + "\r\n\r\nPress any key to return.\r\n")
                output.flush()
                os.read(fd, 1)
            elif key in (b"\r", b"\n"):
                return panes[selected]
            elif len(key) == 1 and b"0" <= key <= b"9":
                index = key[0] - ord("0")
                if index < min(10, len(panes)):
                    return panes[index]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        output.write("\x1b[?25h\x1b[?1049l")
        output.flush()


def remote_selection(name, host):
    result = remote_client.call(host, {"method": "session.inventory"})
    if result.get("available") is not True:
        raise ValueError("remote zmx inventory unavailable")
    state = result["workspaces"].get(name)
    if state is None:
        raise ValueError(f"workspace {name!r} does not exist on {host}")
    sample = result["observation"]
    source = sample["source"]
    if not isinstance(source, dict) or source != result.get("query_source"):
        raise ValueError("remote inventory origin is unknown")
    live = {item["id"] for item in result["sessions"]
            if item["presence"] == "live" and item["workspace"] == name
            and any(ref["workspace"] == name and ref["role"] == "layout" for ref in item["references"])}
    return state, live, source


def origin_command(host, source, *args, tty=False):
    selector = source.get("socket_selector")
    executable, base, hostname, uid = (source.get(key) for key in ("executable", "socket_base", "host", "uid"))
    if (selector not in (None, "ZMX_DIR", "XDG_RUNTIME_DIR", "TMPDIR") or
            not all(isinstance(value, str) and value.startswith("/") and "\0" not in value
                    for value in (executable, base)) or
            not isinstance(hostname, str) or not hostname or "\0" in hostname or type(uid) is not int or uid < 0):
        raise ValueError("invalid remote session origin")
    zmx = shlex.quote(executable)
    prefix = (
        f'test "$(uname -n)" = {shlex.quote(hostname)} && test "$(id -u)" = {uid} || '
        '{ echo "kzw: SSH origin does not match the inventory" >&2; exit 1; }; '
        "unset ZMX_SESSION ZMX_SESSION_PREFIX ZMX_DIR XDG_RUNTIME_DIR TMPDIR; "
        + (f"export {selector}={shlex.quote(base)}; " if selector else "export TMPDIR=/tmp; ")
    )
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-t" if tty else "-T",
            host, prefix + shlex.join([executable, *args])], prefix, zmx


def attach_on_origin(host, pane, source):
    if not PANE.fullmatch(pane):
        raise ValueError("invalid remote pane")
    command, prefix, zmx = origin_command(host, source, tty=True)
    command[-1] = (
        prefix + f'sessions=$({zmx} list --short) || exit; '
          f'printf "%s\\n" "$sessions" | grep -F -x -- {pane} >/dev/null || '
          '{ echo "kzw: selected session is no longer available" >&2; exit 1; }; '
          f"exec {zmx} attach {pane} /bin/false"
    )
    return run_attachment(command)


def run_attachment(command, env=None):
    """Give zmx the real terminal, then regain it without intercepting keys."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        return subprocess.run(command, env=env, check=False).returncode
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def display_text(text, limit=100):
    return "".join(c if c.isprintable() else "?" for c in text[:limit])


def remote_labels(host, source, live):
    from .process_cwd import reported_cwd

    command, _, _ = origin_command(host, source, "list")
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
    labels = {}
    for line in result.stdout.splitlines():
        fields = dict(field.split("=", 1) for field in line.lstrip().split("\t") if "=" in field)
        pane = fields.get("name")
        if pane in live:
            title = fields.get("kzw_title") or pane
            cwd = reported_cwd(fields.get("cwd", ""), {source["host"].lower().rstrip("."), "", "localhost"})
            labels[pane] = display_text(title) + (f" · {display_text(cwd)}" if cwd else "")
    return labels


def select_remote(name, host, previous, notice):
    initial, live, source = remote_selection(name, host)
    tabs = snapshot_tabs(initial.get("snapshot"), initial.get("panes"))
    labels = {p: p for tab in tabs for p in tab}
    if live:
        try:
            labels.update(remote_labels(host, source, live))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # Metadata is advisory.

    def preview(pane):
        latest, available, current_source = remote_selection(name, host)
        if (latest.get("revision") != initial.get("revision") or current_source != source or pane not in available):
            raise ValueError("pane changed during selection")
        command, _, _ = origin_command(host, source, "history", pane, "--screen")
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=10).stdout

    pane = choose(name, tabs, live, labels=labels, preview=preview, previous=previous, notice=notice)
    if pane is None:
        return None
    latest, live, current_source = remote_selection(name, host)
    if latest.get("revision") != initial.get("revision") or pane not in latest.get("panes", []):
        raise ValueError("workspace changed during selection; try again")
    if source != current_source or pane not in live:
        raise ValueError("selected session origin changed or session is no longer available")
    return pane, attach_on_origin(host, pane, source)


def select_local(name, previous, notice):
    initial = coordinator_snapshot(name)
    tabs = snapshot_tabs(initial.get("snapshot"), initial.get("panes"))
    pane = choose(name, tabs, live_sessions(), labels=process_labels(), previous=previous, notice=notice)
    if pane is None:
        return None
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
    return pane, run_attachment(["zmx", "attach", pane, "/bin/false"], env)


def list_and_attach(name, host=""):
    check_name(name)
    if host and (not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host) or host.startswith("-")):
        raise ValueError("invalid remote host")
    previous, notice = None, ""
    while True:
        selected = select_remote(name, host, previous, notice) if host else select_local(name, previous, notice)
        if selected is None:
            return 0
        previous, status = selected
        notice = (f"Attachment ended with status {status}." if status else "")
