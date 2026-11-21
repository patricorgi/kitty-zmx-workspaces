"""Close only acknowledged exited wrappers; validate inside Kitty's event loop."""

import json

from kittens.tui.handler import result_handler
from kitty.constants import version


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    targets = json.loads(args[1])
    selected = []
    for key, expected in targets.items():
        window = boss.window_id_map.get(int(key))
        if window is None:
            continue
        # Kitty 0.50+ reports the root PID (without launch wrappers) in ls.
        pid = window.child.root_pid if version >= (0, 50, 0) else window.child.pid
        actual = {"id": window.id, "pid": pid, "os_window_id": window.os_window_id,
                  "pane": window.user_vars.get("kzw_pane"), "ended": window.user_vars.get("kzw_attach_ended")}
        if actual != expected or actual["ended"] != "1":
            raise ValueError("pane changed before acknowledged exit close")
        selected.append(window)
    for window in selected:
        boss.mark_window_for_close(window)
    return json.dumps({"closed": sorted(targets)})
