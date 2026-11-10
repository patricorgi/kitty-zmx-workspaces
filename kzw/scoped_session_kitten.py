"""Serialize only the OS window containing a verified managed pane."""

import json

from kittens.tui.handler import result_handler
from kitty.session import default_save_as_session_opts


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    if window is None or len(args) != 2 or str(window.os_window_id) != args[1]:
        raise ValueError("workspace window changed during snapshot")
    tm = boss.os_window_map.get(window.os_window_id)
    if tm is None:
        raise ValueError("workspace OS window no longer exists")
    session = '\n'.join(tm.serialize_state_as_session('', None, default_save_as_session_opts(), is_first=True))
    return json.dumps({'os_window_id': window.os_window_id, 'session': session})
