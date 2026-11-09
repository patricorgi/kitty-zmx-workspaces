"""Notify the supervisor of observable changes, never authorize a close."""

import os


def _notify():
    path = os.environ.get("KZW_CHANGE_SIGNAL")
    if path:
        try:
            with open(path, "a", encoding="utf-8"):
                os.utime(path, None)
        except OSError:
            pass


def on_focus_change(boss, window, data):
    _notify()


def on_resize(boss, window, data):
    _notify()


def on_set_user_var(boss, window, data):
    _notify()


def on_close(boss, window, data):
    # This event does not contain a close reason: it is only a hint to
    # reconcile, never permission to remove a pane or kill a session.
    _notify()
