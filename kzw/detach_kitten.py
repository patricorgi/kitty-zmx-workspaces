"""Detach a managed Kitty workspace through kzw's safe snapshot path."""

import os
import subprocess

from kittens.tui.handler import result_handler


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    name = os.environ.get("KZW_WORKSPACE", "")
    if not name or boss.window_id_map.get(target_window_id) is None:
        boss.show_error("Cannot detach", "Workspace identity or target window is unavailable.")
        return
    cmd = ["kzw", "detach", name]
    origin = os.environ.get("KZW_WORKSPACE_ORIGIN", "")
    if origin:
        cmd += ["--host", origin]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    except OSError as exc:
        boss.show_error("Cannot detach", str(exc))
