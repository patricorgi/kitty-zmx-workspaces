"""Open/focus or explicitly close the window's persistent local companion."""

import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from kittens.tui.handler import result_handler
try:
    from kzw.launcher import executable as kzw_executable
except ModuleNotFoundError:
    from launcher import executable as kzw_executable


def main(args):
    pass


def open_local(command, env, boss):
    from kitty.fast_data_types import add_timer, remove_timer

    output = None
    try:
        output = tempfile.TemporaryFile()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, env=env)
    except OSError as exc:
        if output is not None:
            output.close()
        boss.show_error("Cannot open local tab", str(exc))
        return

    def check(timer_id):
        status = process.poll()
        if status is None:
            return
        remove_timer(timer_id)
        with output:
            if status:
                end = output.seek(0, os.SEEK_END)
                output.seek(max(0, end - 4096))
                detail = output.read(4096).decode("utf-8", "replace").strip()
                detail = "".join(c if c.isprintable() or c in "\n\t" else "?" for c in detail)
                boss.show_error("Cannot open local tab", detail or f"kzw exited with status {status}.")

    add_timer(check, 0.25, True)


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    if args[1:] == ["--install-live"]:
        from kitty.fast_data_types import get_options
        from kitty.options.utils import parse_map
        options = get_options()
        path = str(Path(args[0]).expanduser().resolve())
        mappings = ["--when-focus-on var:kzw_local " + key + " kitten " + path + " close"
                    for key in ("ctrl+shift+w", "ctrl+shift+q")]
        for mapping in mappings:
            definition = next(iter(parse_map(mapping))).resolve_and_copy(options.kitty_mod)
            for keymap in (boss.mappings.keyboard_modes[""].keymap, options.keyboard_modes[""].keymap):
                previous = keymap.get(definition.trigger, [])
                keymap[definition.trigger] = [d for d in previous if
                    d.options.when_focus_on != definition.options.when_focus_on] + [definition]
        return
    window = boss.window_id_map.get(target_window_id)
    if window is None:
        return
    if args[1:] == ["--context"]:
        marker = getattr(window, "current_marker_spec", None)
        manager = boss.os_window_map.get(window.os_window_id)
        for tab in getattr(manager, "tabs", ()):
            for candidate in tab:
                spec = getattr(candidate, "current_marker_spec", None)
                if (isinstance(spec, (tuple, list)) and len(spec) == 2 and spec[0] == "function" and
                        isinstance(spec[1], str) and spec[1].rsplit("/", 1)[-1] == "privacy_marker.py"):
                    marker = spec
        return json.dumps({"marker": marker})
    values = window.user_vars
    local = values.get("kzw_local", "")
    name = values.get("kzw_local_workspace" if local else "kzw_workspace", "")
    origin = values.get("kzw_local_origin" if local else "kzw_origin", "")
    pane = local or values.get("kzw_pane", "")
    if not name or not re.fullmatch(r"kzw-[0-9a-f]{32}", pane):
        boss.show_error("Cannot open local tab", "Select a kzw workspace terminal or its local tab.")
        return
    action = args[1] if len(args) > 1 else "open"
    directory = values.get("kzw_local_directory" if local else "kzw_directory", "")
    if action == "open" and not (Path(directory) / "local-tab-capability.json").is_file():
        boss.show_error("Local tab requires a fresh attachment",
                        "Detach and reattach this workspace first. Its existing sessions will keep running.")
        return
    executable = kzw_executable(window)
    command = [executable, "_local-tab", name, "--window-id", str(window.id), "--host", origin]
    if action == "close":
        if not local or len(list(window.tabref())) != 1:
            boss.show_error("Cannot close", "The local tab identity or layout changed.")
            return
        command += ["--close", local]
    elif action == "detach":
        command = [executable, "detach", name, "--host", origin]
    elif action != "open":
        raise ValueError("unknown local tab action")

    def execute(yes=True):
        if not yes or boss.window_id_map.get(target_window_id) is not window:
            return
        if action == "detach":
            boss.call_remote_control(window, ["kitten", str(Path(args[0]).with_name("detach_kitten.py"))])
            return
        env = os.environ.copy()
        env.pop("KZW_RPC_SOCKET", None)
        if action == "open":
            open_local(command, env, boss)
            return
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, env=env)
        except OSError as exc:
            boss.show_error("Local tab operation failed", str(exc))

    manager = boss.os_window_map.get(window.os_window_id)
    if action == "detach" and manager is not None and getattr(manager, "_kzw_detach_in_progress", None) is not None:
        execute()
        return
    if manager is not None and getattr(manager.tab_bar.draw_func, "_equal_margins", False):
        if not getattr(manager.tab_bar.draw_func, "_kzw_local_program", False):
            from kitty.tab_bar import load_custom_draw_tab, load_custom_draw_tab_module
            load_custom_draw_tab_module.clear_cached()
            load_custom_draw_tab.clear_cached()
            manager.tab_bar.draw_func = load_custom_draw_tab()
    if action == "open":
        execute()
    else:
        message = ("Close the local tab and terminate its local zmx shell and all running programs?"
                   if action == "close" else "Detach this workspace? Remote and local sessions will keep running.")
        boss.confirm(message, execute, window=window, confirm_on_accept=False,
                     confirm_on_cancel=False, title="Confirm local tab operation")
