"""Opt-in disposable Kitty test helper: change display state only."""

import json
from kittens.tui.handler import result_handler


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    manager = boss.os_window_map[int(args[1])]
    if args[2] == "start":
        record = manager._kzw_resource_usage
        manager._kzw_detach_in_progress = {"workspace": record["workspace"], "origin": record["origin"]}
    elif args[2] == "finish":
        manager._kzw_detach_in_progress = None
    else:
        raise ValueError("invalid test action")
    manager.mark_tab_bar_dirty()
    return json.dumps({"changed": True})
