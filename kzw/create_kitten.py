"""Start a serialized managed creation from a Kitty key binding."""

import os
import subprocess

from kittens.tui.handler import result_handler


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    kind = args[1] if len(args) > 1 else ""
    window = boss.window_id_map.get(target_window_id)
    name = os.environ.get("KZW_WORKSPACE", "")
    pane = window.user_vars.get("kzw_pane", "") if window else ""
    if kind not in ("right", "below", "split", "tab") or not name or not pane:
        boss.show_error("Cannot create pane", "The workspace or target pane is not managed by kzw.")
        return
    cmd = ["kzw", "_managed-create", name, kind, str(target_window_id), pane,
           "--host", os.environ.get("KZW_HOST", "")]
    origin = os.environ.get("KZW_WORKSPACE_ORIGIN", "")
    if origin:
        cmd += ["--origin", origin]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    except OSError as exc:
        boss.show_error("Cannot create pane", str(exc))
