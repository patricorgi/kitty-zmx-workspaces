"""Add kzw's plain tab index to a shared Kitty instance, then restore it.

A shared instance is already running, so command-line ``-o`` options from a
forwarded launch do not reach it. This kitten is executed inside Kitty and
temporarily installs the same template used by private kzw instances. The
original values are saved once and restored after the last managed pane in the
instance is gone, so unrelated tabs return to the user's configuration.
"""

import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

from kittens.tui.handler import result_handler

try:  # Kitty 0.50+
    from kitty.boss import get_options
except ImportError:  # pragma: no cover - older Kitty exposes boss.opts
    get_options = None

TEMPLATE = "{title.replace('kzw-pane', '…').replace(' │ ', ' · ')}  {fmt.nobold}{fmt.noitalic}{index}"
PANE = re.compile(r"^kzw-[0-9a-f]{32}$")
ORIGINALS = ("tab_title_template", "active_tab_title_template")


def main(args):
    pass


def read_originals(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe tab-style state")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != set(ORIGINALS):
        raise ValueError("invalid tab-style state")
    if (not isinstance(data["tab_title_template"], str) or
            not (data["active_tab_title_template"] is None or
                 isinstance(data["active_tab_title_template"], str))):
        raise ValueError("invalid tab-style state")
    return data


def write_originals(path, options):
    data = {name: getattr(options, name) for name in ORIGINALS}
    if data["active_tab_title_template"] is not None:
        data["active_tab_title_template"] = str(data["active_tab_title_template"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tab-style-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def managed_pane(boss):
    for manager in boss.os_window_map.values():
        for tab in manager.tabs:
            for window in tab:
                pane = window.user_vars.get("kzw_pane", "")
                if isinstance(pane, str) and PANE.fullmatch(pane):
                    return True
    return False


def options_object(boss):
    if get_options is not None:
        return get_options()
    options = getattr(boss, "opts", None)
    if options is None:
        raise ValueError("this Kitty version does not expose runtime options")
    return options


def apply(options, boss):
    options.tab_title_template = TEMPLATE
    options.active_tab_title_template = TEMPLATE
    if hasattr(boss, "apply_new_options"):
        boss.apply_new_options(options)
    else:  # pragma: no cover - older Kitty
        for manager in boss.os_window_map.values():
            manager.refresh_active_tab_bar()


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    if len(args) != 3 or args[1] not in ("apply", "restore"):
        raise ValueError("usage: tab_style_kitten.py apply|restore STATE")
    path = Path(args[2])
    if path.is_symlink():
        raise ValueError("unsafe tab-style state")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(str(path) + ".lock", "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        options = options_object(boss)
        if args[1] == "apply":
            if not path.exists():
                write_originals(path, options)
            apply(options, boss)
            return "applied"
        if not path.exists():
            return "unchanged"
        originals = read_originals(path)
        if managed_pane(boss):
            return "busy"
        for name in ORIGINALS:
            # Preserve any runtime configuration change made while attached.
            if getattr(options, name) == TEMPLATE:
                setattr(options, name, originals[name])
        if hasattr(boss, "apply_new_options"):
            boss.apply_new_options(options)
        else:  # pragma: no cover - older Kitty
            for manager in boss.os_window_map.values():
                manager.refresh_active_tab_bar()
        path.unlink()
        return "restored"
