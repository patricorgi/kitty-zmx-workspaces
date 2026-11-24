"""Supply Kitty's native remote-file menu with the managed wrapper's SSH alias."""

import json
import re
from pathlib import Path
import weakref

from kittens.tui.handler import result_handler


def wrapper_host(window):
    values = window.user_vars
    host = values.get("kzw_host", "")
    pane = values.get("kzw_pane", "")
    if (not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host) or host.startswith("-") or
            not re.fullmatch(r"kzw-[0-9a-f]{32}", pane) or not values.get("kzw_workspace") or
            values.get("kzw_utility") or values.get("kzw_local")):
        return ""
    # User vars can be set by terminal output. Pin the host from the local
    # root wrapper's argv (or its environment for a plain native new tab).
    child = window.child
    argv = child.cmdline
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(arg, str) for arg in argv):
        return ""
    program = Path(argv[0]).name.lower()
    if program == "kzw" and argv[1:2] == ["pane"]:
        args = argv[2:]
    elif program.startswith("python") and argv[1:3] == ["-m", "kzw.pane"]:
        args = argv[3:]
    else:
        return ""
    if not args:
        env = child.environ
        return host if env.get("KZW_HOST") == host and env.get("KZW_WORKSPACE") == values["kzw_workspace"] else ""
    options = list(zip(args, args[1:]))
    if args.count("--pane") == args.count("--host") == 1 and ("--pane", pane) in options and ("--host", host) in options:
        return host
    return ""


def install(boss, window):
    if (not callable(getattr(window, "handle_remote_file", None)) or "handle_remote_file" in vars(window)
            or not callable(getattr(boss, "run_kitten_with_metadata", None))):
        return False
    try:
        host = wrapper_host(window)
    except (AttributeError, OSError, ValueError):  # Child gone, or unsupported Kitty API.
        return False
    if not host:
        return False
    ref = weakref.ref(window)

    def open_file(netloc, remote_path):
        target = ref()
        if target is None or target.destroyed:
            return
        if (not remote_path.startswith("/") or not remote_path.isprintable() or not netloc.isprintable()):
            boss.show_error("Cannot open remote file", "The link must contain a printable absolute path.")
            return
        # Keep the URL hostname for Kitty's independent hostname/mismatch
        # check. Only the validated alias is used as the SSH destination.
        boss.run_kitten_with_metadata("remote_file", [
            "--hostname", netloc.partition(":")[0], "--path", remote_path,
            "--ssh-connection-data", json.dumps(["ssh", host, 0, ""]),
        ], window=target)

    open_file._kzw_remote_host = host
    window.handle_remote_file = open_file
    return True


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map.get(target_window_id)
    if window is not None:
        install(boss, window)
        return getattr(getattr(window, "handle_remote_file", None), "_kzw_remote_host", "")
    return ""
