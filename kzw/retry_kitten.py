"""Request recovery only in the selected, identity-checked workspace view."""

import json
import re

from kittens.tui.handler import result_handler

try:
    from kzw.connection import request_retry
except ModuleNotFoundError as exc:
    if exc.name != "kzw":
        raise
    from connection import request_retry


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    variables = getattr(window, "user_vars", {})
    pane = variables.get("kzw_pane", "")
    workspace, origin = variables.get("kzw_workspace"), variables.get("kzw_origin", "")
    directory = variables.get("kzw_directory", "")
    if not workspace or not re.fullmatch(r"kzw-[0-9a-f]{32}", pane) or variables.get("kzw_utility"):
        return False
    manager = boss.os_window_map.get(window.os_window_id)
    if manager is None:
        return False
    windows = [w for tab in manager.tabs for w in tab if not w.user_vars.get("kzw_utility")]
    if any(w.user_vars.get("kzw_workspace") != workspace or w.user_vars.get("kzw_origin", "") != origin
           or w.user_vars.get("kzw_directory", "") != directory
           or not re.fullmatch(r"kzw-[0-9a-f]{32}", w.user_vars.get("kzw_pane", "")) for w in windows):
        return False
    record = getattr(manager, "_kzw_resource_usage", {})
    endpoints = []
    if (record.get("workspace") == workspace and record.get("origin") == origin and
            record.get("directory", "") == directory and record.get("retry")):
        endpoints.append((record["retry"], ""))
    for candidate in windows:
        values = candidate.user_vars
        if values.get("kzw_connection") not in ("stalled", "reconnecting"):
            continue
        try:
            endpoints.append((json.loads(values.get("kzw_retry_endpoint", "")), values["kzw_pane"]))
        except (ValueError, TypeError):
            continue
    requested = False
    for endpoint, pane_id in endpoints:
        try:
            requested = request_retry(endpoint, workspace, origin, pane_id, directory) or requested
        except (OSError, ValueError, TypeError):
            pass  # Old, exited or replaced wrappers are not signal targets.
    manager.mark_tab_bar_dirty()
    return requested
