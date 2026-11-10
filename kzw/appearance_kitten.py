"""Read appearance from the Kitty window that launched kzw."""

import json

from kittens.tui.handler import result_handler
from kitty.fast_data_types import os_window_font_size


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    if window is None:
        raise ValueError("source Kitty window no longer exists")
    colors = boss.call_remote_control(window, ("get-colors", "--match", f"id:{window.id}"))
    return json.dumps({"font_size": os_window_font_size(window.os_window_id), "colors": colors,
                       "marker": window.current_marker_spec})
