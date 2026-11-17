"""Include verified managed terminals without changing their lifecycle callbacks."""

from contextlib import contextmanager
from functools import partial
from pathlib import Path

try:
    from .core import PANE
except ImportError:
    from core import PANE


HOOKS = ("actions_on_close", "actions_on_focus_change", "actions_on_removal")


class ExportWindow:
    """Read-only serialization view; the real pane keeps its exact hook lists."""

    actions_on_close = ()
    actions_on_focus_change = ()
    actions_on_removal = ()

    def __init__(self, window):
        self.window = window

    def __getattr__(self, name):
        return getattr(self.window, name)


def managed_terminal(window):
    values = window.user_vars
    pane, host = values.get("kzw_pane", ""), values.get("kzw_host", "")
    spec = getattr(window, "creation_spec", None)
    if (not isinstance(pane, str) or not PANE.fullmatch(pane) or spec is None or
            spec.overlay_for is not None or getattr(window, "window_custom_type", "")):
        return False
    command = spec.cmd or []
    # An overlay inheriting pane variables is not a persistent terminal. Require
    # the original pane launcher and its exact stable identity/host arguments.
    if not command:
        return False
    launcher = Path(command[0]).name
    if launcher != "kzw-pane" and not (
            launcher.startswith("python") and command[1:3] == ["-m", "kzw.pane"]):
        return False
    return (command.count("--pane") == 1 and command.count("--host") == 1 and
            any(command[i:i + 2] == ["--pane", pane] for i in range(len(command))) and
            any(command[i:i + 2] == ["--host", host] for i in range(len(command))))


@contextmanager
def serializable_managed_windows(windows, serializer=None):
    """Kitty calls this synchronously; restore per-instance methods even on error."""
    patched = []
    try:
        for window in windows:
            if not any(getattr(window, name, ()) for name in HOOKS):
                continue
            if not managed_terminal(window):
                raise ValueError(f"pane {window.id} has lifecycle hooks but is not a verified managed terminal")
            if serializer is None:
                from kitty.window import Window
                serializer = Window.as_launch_command
            method = window.as_launch_command
            if (getattr(method, "__func__", None) is not serializer or
                    "as_launch_command" in vars(window)):
                raise ValueError(f"pane {window.id} has a custom session serializer; refusing to override it")
            window.as_launch_command = partial(serializer, ExportWindow(window))
            patched.append(window)
        yield
    finally:
        for window in reversed(patched):
            del window.as_launch_command
