"""Determine active zmx foreground processes without running shell commands in panes."""

import json
import os
from pathlib import PurePath
import re
import subprocess
import threading

from .core import PANE


def parse_process_details(zmx_list, ps_output):
    """Map session names to foreground process names using their PTY's tpgid.

    zmx's list output identifies the session's child PID. The child's tty and
    foreground process group identify the program currently controlling it.
    Unknown/exited processes are omitted, not replaced with misleading labels.
    """
    sessions = dict(re.findall(r"\bname=(kzw-[0-9a-f]{32})\s+pid=(\d+)(?:\s|$)", zmx_list))
    processes = {}
    for line in ps_output.splitlines():
        fields = line.strip().split(None, 4)
        if len(fields) != 5:
            continue
        try:
            pid, group, foreground = map(int, fields[:3])
        except ValueError:
            continue
        processes[pid] = (group, foreground, fields[3], fields[4])
    result = {}
    for name, child_pid in sessions.items():
        child = processes.get(int(child_pid))
        if not child or child[1] <= 0 or child[2] in ("?", "??", "-"):
            continue
        foreground = processes.get(child[1])
        if not foreground or foreground[0] != child[1] or foreground[2] != child[2]:
            continue
        # Login shells can be displayed by ps as "-bash" or "-zsh".
        title = PurePath(foreground[3]).name
        if title.startswith("-"):
            title = title[1:]
        # Keep terminal-control characters and unexpected huge output out of
        # Kitty's title and remote-control command arguments.
        title = "".join(c for c in title if c.isprintable())[:100]
        if title:
            result[name] = {"name": title, "pid": child[1]}
    return result


def parse_processes(zmx_list, ps_output):
    return {pane: detail["name"] for pane, detail in parse_process_details(zmx_list, ps_output).items()}


def read_process_details(host):
    """Read session/process metadata in one local process or SSH request."""
    script = ("PATH=\"$HOME/.local/bin:$PATH\"; export PATH; "
              "unset ZMX_SESSION ZMX_SESSION_PREFIX; "
              "zmx list || exit; printf '\\n__KZW_PROCESS_LIST__\\n'; "
              "LC_ALL=C ps -A -o pid=,pgid=,tpgid=,tty=,comm=")
    if host:
        if host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host):
            raise ValueError("invalid host")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, script]
    else:
        cmd = ["sh", "-c", script]
    env = os.environ.copy()
    env.pop("ZMX_SESSION", None)
    env.pop("ZMX_SESSION_PREFIX", None)
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10, check=True).stdout
    sessions, separator, processes = out.partition("\n__KZW_PROCESS_LIST__\n")
    if not separator:
        raise ValueError("incomplete process listing")
    return parse_process_details(sessions, processes)


def read_host(host):
    return {pane: detail["name"] for pane, detail in read_process_details(host).items()}


def active_panes(tree):
    """Find each tab's active managed pane, including tabs not in focus."""
    result = {}
    for os_window in tree:
        for tab in os_window.get("tabs", []):
            active = [w for w in tab.get("windows", []) if w.get("is_active")]
            if len(active) != 1:
                continue
            variables = active[0].get("user_vars", {})
            pane, host = variables.get("kzw_pane", ""), variables.get("kzw_host", "")
            if isinstance(pane, str) and PANE.fullmatch(pane) and isinstance(host, str):
                result[tab["id"]] = (host, pane, tab.get("title", ""))
    return result


def refresh(socket, query, stop=None):
    before = active_panes(json.loads(query(socket, "ls")))
    hosts = {}
    for host, _, _ in before.values():
        if host not in hosts:
            try:
                hosts[host] = read_host(host)
            except (OSError, ValueError, subprocess.SubprocessError):
                hosts[host] = {}
    if stop is not None and stop.is_set():
        return
    # Focus can change while SSH is running. Never set a title for a stale pane.
    after = active_panes(json.loads(query(socket, "ls")))
    for tab_id, (host, pane, title) in after.items():
        if stop is not None and stop.is_set():
            return
        previous = before.get(tab_id)
        if previous is None or previous[:2] != (host, pane):
            continue
        desired = hosts.get(host, {}).get(pane)
        if desired and desired != title:
            query(socket, "set-tab-title", "--match", f"id:{tab_id}", desired)


def start_title_monitor(socket, query, interval=2.0):
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                refresh(socket, query, stop)
            except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
                pass  # Socket/startup races are expected; retry on next pass.
            stop.wait(interval)

    threading.Thread(target=run, name="kzw-tab-titles", daemon=True).start()
    return stop
