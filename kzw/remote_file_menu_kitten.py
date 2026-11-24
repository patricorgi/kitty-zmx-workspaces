"""Open the native file-action chooser for a request from the active kzw pane."""

import json
import re

from kittens.tui.handler import result_handler

try:
    from kzw.remote_file_kitten import install as install_link_handler, wrapper_host
except ModuleNotFoundError as exc:
    if exc.name != "kzw":
        raise
    from remote_file_kitten import install as install_link_handler, wrapper_host


REQUEST = "kzw_file_menu"


def on_request(boss, window, data):
    if data.get("key") != REQUEST:
        return
    # A request is an event, not pane state to export or replay on reattach.
    window.user_vars.pop(REQUEST, None)
    handler = getattr(window, "handle_remote_file", None)
    raw = data.get("value")
    if (window.destroyed or boss.active_window is not window or not getattr(handler, "_kzw_remote_host", "")
            or not isinstance(raw, str) or not 0 < len(raw) <= 32768):
        return
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return
    if not isinstance(value, list) or len(value) != 2:
        return
    hostname, path = value
    if (isinstance(hostname, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", hostname)
            and isinstance(path, str) and path.startswith("/") and path.isprintable()
            and path.rsplit("/", 1)[-1] not in ("", ".", "..")):
        # Offers a choice only. The local wrapper supplies the SSH destination.
        handler(hostname, path)


def install(boss, window):
    install_link_handler(boss, window)
    handler = getattr(window, "handle_remote_file", None)
    host = getattr(handler, "_kzw_remote_host", "")
    # Long-running Kitty instances can still hold our original, unmarked
    # closure, including in their cached installer. Never tag a custom handler.
    if (not host and getattr(handler, "__globals__", None) is install_link_handler.__globals__
            and getattr(handler, "__qualname__", "") == "install.<locals>.open_file"):
        try:
            host = wrapper_host(window)
        except (AttributeError, OSError, ValueError):
            return ""
        if host:
            handler._kzw_remote_host = host
    if not isinstance(host, str) or not host:
        return ""
    if not getattr(window, "_kzw_file_menu_installed", False):
        watchers = window.watchers.copy()
        watchers.on_set_user_var.append(on_request)
        window.watchers = watchers
        window._kzw_file_menu_installed = True
    return host


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    return install(boss, window) if window is not None else ""
