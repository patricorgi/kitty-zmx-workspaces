"""Populate exactly the preview window created for this attach."""

import json
import os

from kittens.tui.handler import result_handler


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    nonce, pid, action, value = args[1:]
    window = boss.window_id_map.get(target_window_id)
    if os.getpid() != int(pid) or window is None or window.user_vars.get("kzw_attach_preview") != nonce:
        raise ValueError("attach preview was closed or replaced")
    tab = window.tabref()
    manager = boss.os_window_map.get(window.os_window_id)
    if manager is None or len(manager) != 1 or list(manager)[0] is not tab or list(tab) != [window]:
        raise ValueError("attach preview was modified; refusing to replace it")
    if action == "error":
        tab.set_title("Attach failed")
        tab.new_special_window(boss.create_special_window_for_show_error("kzw attach", value, window.id),
                               copy_colors_from=window)
        return ""
    if action == "close":
        boss.mark_window_for_close(window)
        return ""
    if action == "check":
        return ""
    if action != "populate":
        raise ValueError("invalid attach preview action")
    from kitty.fast_data_types import get_options
    from kitty.session import create_sessions

    path, focus, expected = json.loads(value)
    sessions = list(create_sessions(get_options(), default_session=path))
    if len(sessions) != 1 or not sessions[0].tabs:
        raise ValueError("attach needs exactly one Kitty session layout")
    before = set(boss.window_id_map)
    try:
        manager.add_tabs_from_session(sessions[0])
        if len(manager) != len(sessions[0].tabs) + 1:
            raise ValueError("incomplete Kitty layout")
        if any(not list(t) for t in manager):
            raise ValueError("empty tab in restored layout")
        panes = [(t, w) for t in manager for w in t if w is not window]
        if len(panes) != expected:
            raise ValueError("incomplete Kitty pane restore")
        if focus:
            matching = [(t, w) for t, w in panes if w.user_vars.get("kzw_pane") == focus]
            if len(matching) != 1:
                raise ValueError("saved focused pane is missing")
            focus_tab, focus_window = matching[0]
            manager.set_active_tab(focus_tab)
            focus_tab.set_active_window(focus_window)
    except Exception:
        # Some children may already be attaching. Close only newly created
        # views in this OS window; existing zmx sessions are not terminated.
        for wid, created in tuple(boss.window_id_map.items()):
            if wid not in before and created.os_window_id == window.os_window_id:
                boss.mark_window_for_close(created)
        raise
    boss.mark_window_for_close(window)
    return json.dumps(window.os_window_id)
