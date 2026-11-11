"""Notify the supervisor of observable changes, never authorize a close."""

import os
import json


def _window_value(window, key, environment_key):
    variables = getattr(window, "user_vars", {})
    value = variables.get(key)
    # Shared views own their context, including absent or empty settings.
    # Only legacy private views inherit the Kitty process environment.
    if "kzw_workspace" not in variables and not value:
        value = os.environ.get(environment_key)
    return value if isinstance(value, str) and value else ""


def _inherit_marker(window):
    spec = _window_value(window, "kzw_marker", "KZW_INHERIT_MARKER")
    if not spec or getattr(window, "_kzw_marker_initialized", False):
        return
    # Initialize once per pane, so a later user toggle is respected.
    window.set_marker(json.loads(spec))
    window._kzw_marker_initialized = True


def _notify(window=None):
    path = _window_value(window, "kzw_signal", "KZW_CHANGE_SIGNAL")
    if path:
        try:
            with open(path, "a", encoding="utf-8"):
                os.utime(path, None)
        except OSError:
            pass


def on_focus_change(boss, window, data):
    _inherit_marker(window)
    _notify(window)


def on_resize(boss, window, data):
    _inherit_marker(window)
    _notify(window)


def on_set_user_var(boss, window, data):
    _notify(window)


def on_cmd_startstop(boss, window, data):
    """Expose Kitty-reported command status as non-authoritative view metadata."""
    if data.get("is_start") is True:
        value = "running"
    elif data.get("is_start") is False and type(data.get("exit_status")) is int:
        value = "exit:" + str(data["exit_status"])
    else:
        return
    window.set_user_var("kzw_command_state", value)
    _notify(window)


def on_close(boss, window, data):
    # This event does not contain a close reason: it is only a hint to
    # reconcile, never permission to remove a pane or kill a session.
    _notify(window)
