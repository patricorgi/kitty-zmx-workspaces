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
    variables = getattr(window, "user_vars", {}) if window is not None else {}
    name = variables.get("kzw_workspace", os.environ.get("KZW_WORKSPACE", ""))
    pane = variables.get("kzw_pane", "")
    if kind not in ("right", "below", "split", "tab") or not name or not pane:
        boss.show_error("Cannot create pane", "The workspace or target pane is not managed by kzw.")
        return
    # Window context is authoritative in a shared instance, including an empty
    # local origin. Older private instances can still use their environment.
    host = variables.get("kzw_host", "" if "kzw_workspace" in variables else os.environ.get("KZW_HOST", ""))
    cmd = ["kzw", "_managed-create", name, kind, str(target_window_id), pane, "--host", host]
    cwd = getattr(window.screen, "last_reported_cwd", "")
    if isinstance(cwd, bytes):
        try:
            cwd = cwd.decode("utf-8")
        except UnicodeDecodeError:
            cwd = ""
    if isinstance(cwd, str) and cwd:
        cmd += ["--cwd-uri", cwd]
    origin = variables.get("kzw_origin", "" if "kzw_workspace" in variables else os.environ.get("KZW_WORKSPACE_ORIGIN", ""))
    if origin:
        cmd += ["--origin", origin]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    except OSError as exc:
        boss.show_error("Cannot create pane", str(exc))
