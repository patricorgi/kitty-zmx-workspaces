"""Minimize or restore only the Kitty OS window containing the caller."""

from kittens.tui.handler import result_handler
from kitty.fast_data_types import cocoa_minimize_os_window, focus_os_window


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    if window is None:
        raise ValueError("supervisor Kitty pane no longer exists")
    if len(args) != 2 or args[1] not in ("minimize", "restore"):
        raise ValueError("invalid supervisor window operation")
    if args[1] == "minimize":
        cocoa_minimize_os_window(window.os_window_id)
    else:
        focus_os_window(window.os_window_id, True)
    return "ok"
