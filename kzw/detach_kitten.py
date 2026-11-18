"""Detach a managed Kitty workspace through kzw's safe snapshot path."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from kittens.tui.handler import result_handler
from kitty.fast_data_types import add_timer, remove_timer


def refresh_renderer(manager):
    """Refresh only the compatible custom renderer, never Kitty's keymaps."""
    bar = getattr(manager, "tab_bar", None)
    draw = getattr(bar, "draw_func", None)
    if getattr(draw, "_equal_margins", False) and not getattr(draw, "_kzw_detaching", False):
        try:
            from kitty.tab_bar import load_custom_draw_tab, load_custom_draw_tab_module
            load_custom_draw_tab_module.clear_cached()
            load_custom_draw_tab.clear_cached()
            bar.draw_func = load_custom_draw_tab()
        except (ImportError, AttributeError):
            pass


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    variables = getattr(window, "user_vars", {}) if window is not None else {}
    local = bool(variables.get("kzw_local"))
    name = variables.get("kzw_local_workspace" if local else "kzw_workspace",
                         os.environ.get("KZW_WORKSPACE", ""))
    if not name or window is None:
        boss.show_error("Cannot detach", "Workspace identity or target window is unavailable.")
        return
    manager = boss.os_window_map.get(window.os_window_id)
    if manager is None:
        boss.show_error("Cannot detach", "Workspace window is unavailable.")
        return
    if getattr(manager, "_kzw_detach_in_progress", None) is not None:
        return
    executable = shutil.which("kzw") or str(Path.home() / ".local/bin/kzw")
    cmd = [executable, "detach", name]
    origin_key = "kzw_local_origin" if local else "kzw_origin"
    origin = variables.get(origin_key, "" if "kzw_workspace" in variables or local else os.environ.get("KZW_WORKSPACE_ORIGIN", ""))
    if origin:
        cmd += ["--host", origin]
    env = os.environ.copy()
    env.pop("KZW_RPC_SOCKET", None)
    env["KZW_WORKSPACE"] = name
    env["KZW_WORKSPACE_ORIGIN"] = origin
    for tab in manager.tabs:
        for candidate in tab:
            values = candidate.user_vars
            if values.get("kzw_workspace") != name or values.get("kzw_origin", "") != origin:
                continue
            child_env = getattr(getattr(candidate, "child", None), "environ", {})
            broker = child_env.get("KZW_RPC_SOCKET", "") if isinstance(child_env, dict) else ""
            if broker:
                env["KZW_RPC_SOCKET"] = broker
                break
        if "KZW_RPC_SOCKET" in env:
            break
    try:
        output = tempfile.TemporaryFile(mode="w+b")
    except OSError as exc:
        boss.show_error("Cannot detach", str(exc))
        return
    try:
        process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, env=env)
    except OSError as exc:
        output.close()
        boss.show_error("Cannot detach", str(exc))
        return
    operation = {"process": process, "output": output, "workspace": name, "origin": origin}
    manager._kzw_detach_in_progress = operation
    refresh_renderer(manager)
    manager.mark_tab_bar_dirty()

    def check(timer_id):
        if getattr(manager, "_kzw_detach_in_progress", None) is not operation:
            remove_timer(timer_id)
            output.close()
            return
        status = process.poll()
        if status is None:
            return
        remove_timer(timer_id)
        manager._kzw_detach_in_progress = None
        if boss.os_window_map.get(window.os_window_id) is manager:
            manager.mark_tab_bar_dirty()
        output.seek(0, os.SEEK_END)
        length = output.tell()
        output.seek(max(0, length - 8192))
        detail = output.read().decode("utf-8", "replace").strip()
        detail = "".join(c if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160)
                         else "?" for c in detail)
        output.close()
        if status:
            # Do not claim the window is still open: a failure may happen after
            # Kitty has already closed it during supervisor cleanup.
            text = detail or f"kzw detach exited with status {status}."
            boss.show_error("Workspace detach failed", text)

    operation["timer"] = add_timer(check, 0.25, True)
