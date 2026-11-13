"""Forward advisory host metrics to the exact attached Kitty workspace."""

import json
from pathlib import Path


def publish(directory, workspace, host, metrics):
    from . import cli
    # The kitten validates both the workspace and the origin inside Kitty;
    # a stale OS-window ID or a focus change cannot retarget this update.
    view = cli.view_state(directory)
    if view is not None:
        os_id = view["os_window_id"]
    else:
        tree = json.loads(cli.view_query(directory, "ls"))
        if len(tree) != 1:
            return
        os_id = tree[0]["id"]
    payload = json.dumps({"workspace": workspace, "origin": host, "metrics": metrics}, separators=(",", ":"))
    cli.view_query(directory, "kitten", str(Path(__file__).with_name("metrics_kitten.py")), str(os_id), payload)
