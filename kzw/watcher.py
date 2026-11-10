"""Notify the supervisor of observable changes, never authorize a close."""

import os
import json


def _inherit_marker(window):
    spec = os.environ.get("KZW_INHERIT_MARKER")
    if not spec or getattr(window, "_kzw_marker_initialized", False):
        return
    # Initialize once per pane, so a later user toggle is respected.
    window.set_marker(json.loads(spec))
    window._kzw_marker_initialized = True


def _notify():
    path = os.environ.get("KZW_CHANGE_SIGNAL")
    if path:
        try:
            with open(path, "a", encoding="utf-8"):
                os.utime(path, None)
        except OSError:
            pass


def on_focus_change(boss, window, data):
    _inherit_marker(window)
    _notify()


def on_resize(boss, window, data):
    _inherit_marker(window)
    _notify()


def on_set_user_var(boss, window, data):
    _notify()


def on_cmd_startstop(boss, window, data):
    """Expose Kitty-reported command status as non-authoritative view metadata."""
    if data.get("is_start") is True:
        value = "running"
    elif data.get("is_start") is False and type(data.get("exit_status")) is int:
        value = "exit:" + str(data["exit_status"])
    else:
        return
    window.set_user_var("kzw_command_state", value)
    _notify()


def on_close(boss, window, data):
    # This event does not contain a close reason: it is only a hint to
    # reconcile, never permission to remove a pane or kill a session.
    _notify()
