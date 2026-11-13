"""Serialize only the OS window containing a verified managed pane."""

import json

from kittens.tui.handler import result_handler
from kitty.session import default_save_as_session_opts

# Kitty supplies the script directory on sys.path, but does not define __file__.
try:
    from kzw.utility_tabs import workspace_tree
except ModuleNotFoundError as exc:
    if exc.name != "kzw":
        raise
    from utility_tabs import workspace_tree


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    if (window is None or len(args) not in (2, 3) or str(window.os_window_id) != args[1] or
            (len(args) == 3 and args[2] != "--with-tree")):
        raise ValueError("workspace window changed during snapshot")
    tm = boss.os_window_map.get(window.os_window_id)
    if tm is None:
        raise ValueError("workspace OS window no longer exists")
    has_utilities = any("kzw_utility" in w.user_vars for tab in tm.tabs for w in tab)
    tree = None
    matched = None
    if has_utilities or len(args) == 3:
        tree = workspace_tree(list(boss.list_os_windows(tab_filter=lambda tab: tab.os_window_id == window.os_window_id)))
    if has_utilities:
        if len(tree) != 1:
            raise ValueError("workspace changed during utility tab snapshot")
        matched = frozenset(boss.window_id_map[w["id"]] for tab in tree[0]["tabs"] for w in tab["windows"])
    lines = list(tm.serialize_state_as_session('', matched, default_save_as_session_opts(), is_first=True))
    if has_utilities:
        active = [i for i, tab in enumerate(tree[0]["tabs"]) if tab.get("is_active")]
        if len(active) != 1:
            raise ValueError("cannot identify workspace focus behind utility tab")
        # Kitty's manager uses the unfiltered tab index, even with matched_windows.
        lines = [line for line in lines if not line.startswith("focus_tab ")]
        lines.append("focus_tab " + str(active[0]))
    session = '\n'.join(lines)
    result = {'os_window_id': window.os_window_id, 'session': session}
    if len(args) == 3:
        # Both representations are read in one Kitty event-loop callback, so
        # layout/focus changes cannot interleave separate remote-control reads.
        result['tree'] = tree
    return json.dumps(result)
