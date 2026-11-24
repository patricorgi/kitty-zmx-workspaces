"""Serialize only the OS window containing a verified managed pane."""

import json
import importlib.util

from kittens.tui.handler import result_handler
from kitty.session import default_save_as_session_opts

# Kitty supplies the script directory on sys.path, but does not define __file__.
try:
    from kzw.core import pane_map, rewrite_export
    from kzw.session_export import serializable_managed_windows
    from kzw.remote_file_menu_kitten import install as install_remote_file
    from kzw import utility_tabs
except ModuleNotFoundError as exc:
    if exc.name != "kzw":
        raise
    from core import pane_map, rewrite_export
    from session_export import serializable_managed_windows
    from remote_file_menu_kitten import install as install_remote_file
    import utility_tabs


def current_workspace_tree():
    """Use the local-tab-aware adapter even in a long-running Kitty instance.

    Do not reload or replace a shared module: other Kitty components can retain
    references to it. The existing module's trusted source path identifies the
    adapter distributed alongside this kitten.
    """
    if hasattr(utility_tabs, "is_local"):
        return utility_tabs.workspace_tree
    path = getattr(utility_tabs, "__file__", "")
    if not path:
        raise ValueError("local-tab-aware workspace projection is unavailable")
    spec = importlib.util.spec_from_file_location("_kzw_scoped_utility_tabs", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load local-tab-aware workspace projection")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "is_local"):
        raise ValueError("local-tab-aware workspace projection is unavailable")
    return module.workspace_tree


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
    has_utilities = any("kzw_utility" in w.user_vars or "kzw_local" in w.user_vars for tab in tm.tabs for w in tab)
    tree = current_workspace_tree()(list(boss.list_os_windows(tab_filter=lambda tab: tab.os_window_id == window.os_window_id)))
    if len(tree) != 1 or tree[0]["id"] != window.os_window_id:
        raise ValueError("workspace changed during scoped snapshot")
    panes = pane_map(tree)
    windows = [boss.window_id_map[window_id] for window_id in panes]
    for managed in windows:
        install_remote_file(boss, managed)
    matched = None
    if has_utilities:
        matched = frozenset(boss.window_id_map[w["id"]] for tab in tree[0]["tabs"] for w in tab["windows"])
    with serializable_managed_windows(windows):
        lines = list(tm.serialize_state_as_session('', matched, default_save_as_session_opts(), is_first=True))
    if has_utilities:
        active = [i for i, tab in enumerate(tree[0]["tabs"]) if tab.get("is_active")]
        if len(active) != 1:
            raise ValueError("cannot identify workspace focus behind utility tab")
        # Kitty's manager uses the unfiltered tab index, even with matched_windows.
        lines = [line for line in lines if not line.startswith("focus_tab ")]
        lines.append("focus_tab " + str(active[0]))
    session = '\n'.join(lines)
    # Validate completeness before returning; the caller still rewrites all
    # programs to existing-session attachment and the server validates ownership.
    rewrite_export(session, panes, "kzw")
    result = {'os_window_id': window.os_window_id, 'session': session}
    if len(args) == 3:
        # Both representations are read in one Kitty event-loop callback, so
        # layout/focus changes cannot interleave separate remote-control reads.
        result['tree'] = tree
    return json.dumps(result)
