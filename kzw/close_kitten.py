"""Confirmation for kzw's explicitly mapped Kitty close actions."""

import json
import os
import subprocess

from kittens.tui.handler import result_handler


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    kind = args[1] if len(args) > 1 else ""
    window = boss.window_id_map.get(target_window_id)
    if kind not in ("pane", "tab") or window is None:
        return
    tab = boss.tab_for_id(window.tab_id)
    if tab is None:
        return
    selected = [window] if kind == "pane" else list(tab)
    selection = {str(w.id): [w.user_vars.get("kzw_pane", ""), w.user_vars.get("kzw_host", "")]
                 for w in selected}
    if not selection or any(not value[0] for value in selection.values()):
        boss.show_error("Cannot close", "The selected panes are not all managed by kzw.")
        return
    target = window.id if kind == "pane" else tab.id
    variables = window.user_vars
    name = variables.get("kzw_workspace", os.environ.get("KZW_WORKSPACE", ""))
    origin = variables.get("kzw_origin", "" if "kzw_workspace" in variables else os.environ.get("KZW_WORKSPACE_ORIGIN", ""))
    if not name:
        boss.show_error("Cannot close", "Workspace identity is unavailable.")
        return
    count = len(selection)
    message = (f"Close this {kind} and terminate {count} shell(s) and their running programs?\n"
               "This cannot be undone.")
    if len(selection) == len(boss.window_id_map):
        message += "\nThe workspace will be empty."

    def confirmed(yes):
        if not yes:
            return
        cmd = ["kzw", "_confirmed-close", name, kind, str(target), json.dumps(selection)]
        if origin:
            cmd += ["--host", origin]
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        except OSError as exc:
            boss.show_error("Cannot close", str(exc))

    boss.confirm(message, confirmed, window=window, confirm_on_accept=False,
                 confirm_on_cancel=False, title="Confirm managed close")
