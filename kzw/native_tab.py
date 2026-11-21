"""Kitty 0.51 launch wrapper for plain native tabs in a shared kzw view."""

import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {
    "KZW_HOST": "kzw_host",
    "KZW_WORKSPACE": "kzw_workspace",
    "KZW_WORKSPACE_ORIGIN": "kzw_origin",
    "KZW_WORKSPACE_DIRECTORY": "kzw_directory",
    "KZW_CHANGE_SIGNAL": "kzw_signal",
    "KZW_WORKSPACE_COORDINATOR": "kzw_coordinator",
    "KZW_INHERIT_MARKER": "kzw_marker",
}


def prefix_for_launch(ctx):
    # Kitty calls this on its UI thread. Do not query Kitty or the coordinator here.
    if ctx.launch_type != "tab" or not ctx.is_default_shell or ctx.user_vars or ctx.source_window_id is None:
        return None
    from kitty.fast_data_types import get_boss

    source = get_boss().window_id_map.get(ctx.source_window_id)
    if source is None:
        return None
    variables = source.user_vars
    if not (variables.get("kzw_pane") and variables.get("kzw_workspace") and variables.get("kzw_directory")):
        return None
    def matches(env):
        return all(env.get(key, "") == variables.get(var, "") for key, var in CONTEXT.items()
                   if key != "KZW_INHERIT_MARKER")

    child = source.child.final_env
    if not matches(child):
        # A previously wrapped tab gained its workspace environment on exec,
        # after Kitty recorded final_env for the original command.
        child = source.child.environ
    if not matches(child):
        return None
    python = shutil.which("python3", path=child.get("PATH") or os.defpath)
    if python is None:
        return None
    keys = (*CONTEXT, "KZW_RPC_SOCKET", "KZW_SSH_KITTEN", "KZW_CONNECTION_DISPLAY",
            "KZW_DEBUG", "XDG_STATE_HOME", "XDG_CACHE_HOME", "ZMX_DIR", "PATH")
    # The workspace's launcher environment may differ from Kitty's, including
    # its broker/socket and the local session namespace. Never inherit these
    # from whatever application happened to start the shared Kitty instance.
    context = {key: child[key] for key in keys if child.get(key)}
    return [python, str(ROOT / "kzw/native_tab.py"), str(os.getpid()),
            str(source.id), variables["kzw_pane"], json.dumps(context), "--"]


def check_target(view, tree, instance_pid, source_id, pane, target_id, env):
    if view is None or view["instance_pid"] != instance_pid or view["address"] != env.get("KITTY_LISTEN_ON"):
        return False
    if source_id == target_id:
        return False
    window = next((w for w in tree if w["id"] == view["os_window_id"]), None)
    if window is None:
        return False
    source = target = None
    for tab in window["tabs"]:
        for w in tab["windows"]:
            if w["id"] == source_id:
                source = tab["id"], w.get("user_vars", {})
            if w["id"] == target_id:
                target = tab["id"], tab["windows"], w.get("user_vars", {})
    return bool(source and target and source[0] != target[0] and len(target[1]) == 1 and
                not target[2].get("kzw_pane") and source[1].get("kzw_pane") == pane and
                all(source[1].get(var, "") == env.get(key, "") for key, var in CONTEXT.items()
                    if key != "KZW_INHERIT_MARKER"))


def run():
    # This process runs in the new terminal, not in Kitty. Verify both ends
    # before allocating an identity or starting any zmx session.
    import subprocess

    sys.path.insert(0, str(ROOT))
    from kzw.cli import view_state
    from kzw.core import PANE
    from kzw.kitty_instance import remote_raw

    instance_pid, source_id, pane, encoded, _, *original = sys.argv[1:]
    workspace = json.loads(encoded)
    context = {key: val for key, val in os.environ.items() if not key.startswith("KZW_") and
               key not in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "ZMX_DIR")}
    context.update(workspace)
    try:
        target_id = int(context["KITTY_WINDOW_ID"])
        source_id, instance_pid = int(source_id), int(instance_pid)
        if not PANE.fullmatch(pane):
            raise ValueError("invalid source pane ID")
        view = view_state(Path(context["KZW_WORKSPACE_DIRECTORY"]))
        if view is not None and view["instance_pid"] == instance_pid and view["address"] == context.get("KITTY_LISTEN_ON"):
            # Match whole tabs, not just the two panes: the target must be a singleton.
            tree = json.loads(remote_raw(view["address"], "ls", "--match-tab",
                                         f"window_id:{source_id} or window_id:{target_id}"))
            if check_target(view, tree, instance_pid, source_id, pane, target_id, context):
                executable = str(ROOT / "bin/kzw-pane")
                return os.execve(executable, [executable], context)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"kzw: native tab not managed: {exc}", file=sys.stderr)
    # Keep the native command when the action targeted a different OS window.
    os.execvp(original[0], original)


if __name__ == "__main__":
    run()
