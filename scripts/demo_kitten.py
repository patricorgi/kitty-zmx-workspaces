"""Apply the user's privacy marker only to a completely fenced demo window."""

import json
import os

from kittens.tui.handler import result_handler


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    request = json.loads(args[1])
    anchor = boss.window_id_map.get(target_window_id)
    if anchor is None or anchor.os_window_id != request["os_window_id"]:
        raise ValueError("demo window identity changed")
    manager = boss.os_window_map[anchor.os_window_id]
    windows = [window for tab in manager.tabs for window in tab]
    if (not windows or {window.id for window in windows} != set(request["windows"]) or
            any(window.user_vars.get("kzw_workspace") != request["name"] or
                window.user_vars.get("kzw_host") != request["host"] for window in windows)):
        raise ValueError("demo contains an unexpected pane; nothing changed")
    spec = ["function", request["marker"]]
    # Check the complete window before mutating any pane, then set, never toggle.
    for window in windows:
        window.set_marker(spec)
        window.set_user_var("kzw_marker", json.dumps(spec))
    tabs = []
    for tab in manager.tabs:
        panes = []
        for window in tab:
            if list(window.current_marker_spec or ()) != spec:
                raise ValueError("privacy marker did not initialize")
            geometry = window.geometry
            panes.append({"id": window.id, "pane": window.user_vars["kzw_pane"],
                          "rect": [geometry.left, geometry.top, geometry.right, geometry.bottom],
                          "active": window is tab.active_window})
        tabs.append({"id": tab.id, "layout": tab.current_layout.name,
                     "active": tab is manager.active_tab, "panes": panes})
    return json.dumps({"pid": os.getpid(), "os_window_id": anchor.os_window_id, "tabs": tabs})
