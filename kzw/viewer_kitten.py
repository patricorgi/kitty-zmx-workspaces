"""Handle a Yazi preview request only in its active, verified remote pane."""

import json
import re
import sys
import subprocess
from threading import Event, Thread

from kittens.tui.handler import result_handler
from kitty.fast_data_types import add_timer

try:
    from kzw.viewer_files import destination, open_image, watch
    from kzw.remote_file_kitten import wrapper_host
except ModuleNotFoundError as exc:
    if exc.name not in ("kzw", "kzw.viewer_files"):
        raise
    from viewer_files import destination, open_image, watch
    from remote_file_kitten import wrapper_host


def cancel_job(window, attr):
    current = getattr(window, attr, None)
    if current:
        current[1].set()
        setattr(window, attr, None)


def stop(boss, window, data):
    cancel_job(window, "_kzw_pdf_watch")
    cancel_job(window, "_kzw_image_open")


def on_request(boss, window, data):
    request = data.get("key")
    if request not in ("kzw_live_pdf", "kzw_open_pdf", "kzw_open_image"):
        return
    window.user_vars.pop(request, None)
    if window.destroyed or boss.active_window is not window:
        return
    alias = wrapper_host(window)
    raw = data.get("value")
    if not alias or not isinstance(raw, str) or len(raw) > 32768:
        return
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return
    if not isinstance(value, list) or len(value) != 2:
        return
    host, path = value
    image = request == "kzw_open_image"
    if not (isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", host)
            and isinstance(path, str) and path.startswith("/") and path.isprintable()
            and path.rsplit("/", 1)[-1] not in ("", ".", "..") and (image or path.lower().endswith(".pdf"))):
        return
    key = (alias, host, path)
    attr = "_kzw_image_open" if image else "_kzw_pdf_watch"
    current = getattr(window, attr, None)
    pane = window.user_vars["kzw_pane"]
    if current and current[0] == key and not image:
        if request == "kzw_live_pdf":
            cancel_job(window, attr)
        else:
            dest = destination(*key, pane)
            if dest.is_file():
                subprocess.Popen(["/usr/bin/open", "-a", "Skim", str(dest)])
        return
    cancel_job(window, attr)
    cancel = Event()
    state = (key, cancel)
    setattr(window, attr, state)
    errors = []

    def run():
        try:
            (open_image if image else watch)(*key, pane, cancel)
        except Exception as exc:
            errors.append(str(exc))

    thread = Thread(target=run, daemon=True)
    thread.start()

    def check(timer):
        if window.destroyed or getattr(window, attr) is not state:
            cancel.set()
        elif thread.is_alive():
            add_timer(check, 2, False)
        else:
            setattr(window, attr, None)
            if errors:
                boss.show_error("Image preview" if image else "Live PDF preview", errors[0])
    add_timer(check, 1, False)


def install(boss, window):
    if sys.platform != "darwin" or getattr(window, "_kzw_viewer_installed", False):
        return
    if not getattr(getattr(window, "handle_remote_file", None), "_kzw_remote_host", ""):
        return
    watchers = window.watchers.copy()
    # Replace only the previous kzw PDF watcher in already-attached panes.
    for hooks in (watchers.on_set_user_var, watchers.on_close):
        hooks[:] = [f for f in hooks if not (
            getattr(getattr(f, "__code__", None), "co_filename", "").endswith("/live_pdf_kitten.py")
            and getattr(f, "__name__", "") in ("on_request", "stop"))]
    watchers.on_set_user_var.append(on_request)
    watchers.on_close.append(stop)
    window.watchers = watchers
    window._kzw_viewer_installed = True


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    if window:
        install(boss, window)
        return bool(getattr(window, "_kzw_viewer_installed", False))
