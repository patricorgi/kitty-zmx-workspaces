"""Close only acknowledged exited wrappers; validate inside Kitty's event loop."""

import json

from kittens.tui.handler import result_handler


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
        actual = {"id": window.id, "pid": window.child.pid, "os_window_id": window.os_window_id,
                  "pane": window.user_vars.get("kzw_pane"), "ended": window.user_vars.get("kzw_attach_ended")}
        if actual != expected or actual["ended"] != "1":
            raise ValueError("pane changed before acknowledged exit close")
        selected.append(window)
    for window in selected:
        boss.mark_window_for_close(window)
    return json.dumps({"closed": sorted(targets)})
