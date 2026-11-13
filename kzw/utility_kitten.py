"""Create a coordinator-owned queue tab through normal managed creation."""

import os
from pathlib import Path
import re
import shutil
import subprocess

from kittens.tui.handler import result_handler


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    if window is None:
        return
    values = window.user_vars
    if values.get("kzw_utility") == "condor-queue":
        return  # Already in the picker; never nest utility tabs.
    pane, workspace = values.get("kzw_pane", ""), values.get("kzw_workspace", "")
    host = values.get("kzw_host", "")
    if (not re.fullmatch(r"kzw-[0-9a-f]{32}", pane) or not workspace or
            (host and (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host)))):
        boss.show_error("Cannot open queue", "Select a managed kzw terminal with a valid origin host.")
        return
    origin = values.get("kzw_origin", "")
    executable = shutil.which("kzw") or str(Path.home() / ".local/bin/kzw")
    command = [executable, "_managed-create", workspace, "tab", str(window.id), pane,
               "--host", host, "--initial-program", "condor-queue"]
    if origin:
        command += ["--origin", origin]
    env = os.environ.copy()
    env.pop("KZW_RPC_SOCKET", None)
    env["KZW_WORKSPACE"] = workspace
    env["KZW_WORKSPACE_ORIGIN"] = origin
    # The shared Kitty server's inherited broker may belong to another workspace.
    candidates = [window] + [w for w in boss.window_id_map.values() if w is not window]
    for candidate in candidates:
        context = candidate.user_vars
        if (getattr(candidate, "os_window_id", None) != getattr(window, "os_window_id", None) or
                context.get("kzw_workspace") != workspace or context.get("kzw_origin", "") != origin or
                not context.get("kzw_pane")):
            continue
        child_env = getattr(getattr(candidate, "child", None), "environ", {})
        broker = child_env.get("KZW_RPC_SOCKET", "") if isinstance(child_env, dict) else ""
        if broker:
            env["KZW_RPC_SOCKET"] = broker
            break
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, env=env)
    except OSError as exc:
        boss.show_error("Cannot open queue", str(exc))
