"""Transform Kitty's layout export into safe zmx reattachment commands."""

import json
import os
import re
import shlex
import tempfile
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
PANE = re.compile(r"^kzw-[0-9a-f]{32}$")
FLAG = "kitty-unserialize-data="
FOCUS_PREFIX = "# kzw-focus "


def check_name(name):
    if not NAME.fullmatch(name):
        raise ValueError("workspace name must contain only letters, numbers, '.', '_' or '-'")
    return name


def pane_map(tree):
    result = {}
    for os_window in tree:
        for tab in os_window.get("tabs", []):
            for window in tab.get("windows", []):
                variables = window.get("user_vars", {})
                pane = variables.get("kzw_pane", "")
                host = variables.get("kzw_host", "")
                if not PANE.fullmatch(pane):
                    raise ValueError("a Kitty pane is not managed by kzw; refusing to save an incomplete workspace")
                if host and (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host)):
                    raise ValueError("invalid SSH host in pane metadata")
                result[int(window["id"])] = (pane, host)
    if not result or len({v[0] for v in result.values()}) != len(result):
        raise ValueError("workspace has no panes or duplicate pane identities")
    return result


def focused_pane(tree):
    """Return the active pane of Kitty's last focused OS window."""
    last = [window for window in tree if window.get("last_focused")]
    if not last:
        last = [window for window in tree if window.get("is_focused")]
    if len(last) != 1:
        raise ValueError("cannot identify Kitty's last focused OS window")
    tabs = [tab for tab in last[0].get("tabs", []) if tab.get("is_active")]
    if len(tabs) != 1:
        raise ValueError("cannot identify the active Kitty tab")
    windows = [window for window in tabs[0].get("windows", []) if window.get("is_active")]
    if len(windows) != 1:
        raise ValueError("cannot identify the active Kitty pane")
    pane = windows[0].get("user_vars", {}).get("kzw_pane", "")
    if not PANE.fullmatch(pane):
        raise ValueError("focused pane is not managed by kzw")
    return pane


def saved_focus(snapshot):
    """Read an optional stable pane identity from a Kitty session comment."""
    for line in snapshot.splitlines():
        if line.startswith(FOCUS_PREFIX):
            value = json.loads(line[len(FOCUS_PREFIX):])
            pane = value.get("pane", "")
            if not PANE.fullmatch(pane):
                raise ValueError("invalid saved focus metadata")
            return pane
    return None


def rewrite_export(text, panes, pane_executable):
    """Keep Kitty layout state, replace every serialized program with attach.

    Kitty 0.49 uses kitty-unserialize-data with the original window id. Reject
    unknown syntax rather than saving a snapshot that might run a command twice.
    """
    lines, seen = [], set()
    for line in text.splitlines():
        if not line.startswith("launch "):
            lines.append(line)
            continue
        args = shlex.split(line)
        if len(args) < 2 or not args[1].startswith(FLAG):
            raise ValueError("unsupported Kitty session export: missing serialized window ID")
        data = json.loads(args[1][len(FLAG):])
        window_id = int(data["id"])
        if window_id not in panes or window_id in seen:
            raise ValueError("layout changed during snapshot; retrying")
        seen.add(window_id)
        # Never replay Kitty's optional foreground command.
        data.pop("cmd_at_shell_startup", None)
        options = []
        for arg in args[2:]:
            if not arg.startswith("--"):
                break
            # Kitty's serializer writes every option as --name=value or a flag.
            if not arg.startswith(("--var=kzw_attach_ended=", "--var=kzw_staging=")):
                options.append(arg)
        pane, host = panes[window_id]
        command = [pane_executable, "--pane", pane, "--host", host, "--existing"]
        lines.append(shlex.join(["launch", FLAG + json.dumps(data, separators=(",", ":")), *options, *command]))
    if seen != set(panes):
        raise ValueError("layout export did not include every managed pane")
    return "\n".join(lines) + "\n"


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
