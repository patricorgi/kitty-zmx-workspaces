"""Inspect rendering only in the explicitly selected disposable Kitty window."""

import json
import runpy

from kittens.tui.handler import result_handler


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    manager = boss.os_window_map[int(args[1])]
    bar = manager.tab_bar
    if len(args) > 2 and args[2] == "install":
        module = runpy.run_path(args[3])
        bar.draw_func = module["draw_tab"]
        bar.draw_func_is_custom = True
    columns = bar.screen.columns
    if len(args) > 2 and args[2] == "narrow":
        bar.screen.resize(1, 30)
    try:
        bar.update(manager.tab_bar_data)
        return json.dumps({"record": getattr(manager, "_kzw_resource_usage", None),
                           "line": str(bar.screen.line(0)), "columns": bar.screen.columns,
                           "fg": [bar.screen.line(0).cursor_from(i).fg >> 8 for i in range(bar.screen.columns)],
                           "extents": [repr(extent) for extent in bar.tab_extents],
                           "other_records": {str(key): getattr(value, "_kzw_resource_usage", None)
                                             for key, value in boss.os_window_map.items() if value is not manager}})
    finally:
        if bar.screen.columns != columns:
            bar.screen.resize(1, columns)
            bar.update(manager.tab_bar_data)
