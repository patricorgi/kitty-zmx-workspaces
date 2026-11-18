"""A singleton local zmx companion associated with a workspace's local view."""

import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import threading
import time
import uuid

from .core import PANE, atomic_write
from . import remote_client
from .utility_tabs import is_local as local_window


def owner(directory, create=False):
    path = directory / "local-tab-owner"
    if path.is_symlink():
        raise ValueError("unsafe local tab owner")
    if not path.exists():
        if not create:
            return None
        atomic_write(path, uuid.uuid4().hex + "\n")
    value = path.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("invalid local tab owner")
    return value


def request(directory, action="get", **values):
    identity = owner(directory)
    if identity is None:
        return {"tab": None}
    # Do not reuse the remote workspace broker or its connection-health record.
    health = getattr(remote_client._local, "connection_health", None)
    remote_client._local.connection_health = None
    try:
        return remote_client.call("", {"method": "local.tab." + action, "owner": identity, **values})
    finally:
        remote_client._local.connection_health = health


def windows(directory):
    """Unfiltered, OS-window-scoped listing, including the companion."""
    from .cli import list_os_windows, view_state
    view = view_state(directory)
    tree = list_os_windows(view["address"] if view else directory / "kitty.sock")
    if view:
        tree = [item for item in tree if item["id"] == view["os_window_id"]]
    else:
        tree = [item for item in tree if any(
            w.get("user_vars", {}).get("kzw_directory") == str(directory) or
            w.get("user_vars", {}).get("kzw_local_directory") == str(directory)
            for tab in item.get("tabs", []) for w in tab.get("windows", []))]
    if len(tree) != 1:
        raise ValueError("cannot identify workspace window for local tab")
    return tree


def launch(directory, name, origin="", target=None, create=False, focus=True):
    """Called under the workspace capture lock, before any Kitty mutation."""
    from .cli import view_query
    identity = owner(directory, create=create)
    if identity is None:
        return
    state = request(directory)["tab"]
    if state is None:
        if not create:
            return
        if not shutil.which("zmx"):
            raise ValueError("local zmx is not installed")
        pane = "kzw-" + uuid.uuid4().hex
        state = request(directory, "reserve", pane=pane)["tab"]
    if focus is None:
        focus = state["focused"]
    tree = windows(directory)
    if state["state"] == "closing":
        raise ValueError("local tab cleanup is pending; close it again or retry kzw kill")
    pane = state["pane"]
    all_windows = [w for tab in tree[0]["tabs"] for w in tab.get("windows", [])]
    if target is not None and not any(w["id"] == target and (
            local_window(w) or (w.get("user_vars", {}).get("kzw_workspace") == name and
                               w.get("user_vars", {}).get("kzw_origin", "") == origin)) for w in all_windows):
        raise ValueError("local tab creation target changed")
    existing = [w for w in all_windows if w.get("user_vars", {}).get("kzw_local_owner") == identity]
    if existing:
        if len(existing) != 1 or not local_window(existing[0]) or existing[0]["user_vars"]["kzw_local"] != pane:
            raise ValueError("local tab identity is incomplete or duplicated")
        if focus:
            view_query(directory, "focus-window", "--match", f"id:{existing[0]['id']}")
        return
    managed = [w for w in all_windows if w.get("user_vars", {}).get("kzw_workspace") == name and
               w.get("user_vars", {}).get("kzw_origin", "") == origin and
               PANE.fullmatch(w.get("user_vars", {}).get("kzw_pane", ""))]
    source = next((w for w in managed if target == w["id"]), None)
    source = source or next((w for w in managed if w.get("is_focused")), None) or next(iter(managed), None)
    if source is None:
        raise ValueError("select a managed workspace terminal before opening a local tab")
    values = {"kzw_local": pane, "kzw_local_owner": identity, "kzw_local_workspace": name,
              "kzw_local_origin": origin, "kzw_local_directory": str(directory),
              "kzw_local_source": str(source["id"]), "kzw_local_source_pane": source["user_vars"]["kzw_pane"]}
    options = ["--type=tab", "--location=last", "--cwd=" + str(Path.home()),
               "--add-to-session=!"]
    context = json.loads(view_query(directory, "kitten", "--match", f"id:{source['id']}",
                                    str(Path(__file__).with_name("local_tab_kitten.py")), "--context"))
    marker = context.get("marker") if isinstance(context, dict) else None
    if (isinstance(marker, list) and len(marker) == 2 and marker[0] == "function" and
            isinstance(marker[1], str)):
        options.append("--marker=" + shlex.join(marker))
    if not focus:
        options.append("--keep-focus")
    options += ["--var=" + key + "=" + value for key, value in values.items()]
    # A separate Kitty instance inherits workspace environment; local shells must
    # not accidentally contact the remote broker or use its shell wrapper.
    options += ["--env=" + key for key in (
        "KZW_HOST", "KZW_WORKSPACE", "KZW_WORKSPACE_ORIGIN", "KZW_WORKSPACE_COORDINATOR",
        "KZW_WORKSPACE_DIRECTORY", "KZW_RPC_SOCKET", "KZW_CHANGE_SIGNAL", "KZW_CONNECTION_DISPLAY",
        "ZMX_SESSION", "ZMX_SESSION_PREFIX")]
    executable = shutil.which("kzw")
    if not executable:
        raise ValueError("kzw is not installed")
    result = view_query(directory, "launch", "--match", f"window_id:{source['id']}", *options,
                        executable, "_local-attach", identity, pane)
    if not result.strip().isdigit():
        raise ValueError("local tab launch outcome unknown; metadata retained")


def sync_focus(directory):
    if owner(directory) is None:
        return
    state = request(directory)["tab"]
    if state is None:
        return
    tree = windows(directory)
    locals_ = [w for tab in tree[0]["tabs"] for w in tab.get("windows", [])
               if w.get("user_vars", {}).get("kzw_local_owner") == owner(directory)]
    if not locals_:
        return  # A vanished view is not an exit or authority to remove metadata.
    if len(locals_) != 1 or not local_window(locals_[0]) or locals_[0]["user_vars"]["kzw_local"] != state["pane"]:
        raise ValueError("local tab identity changed")
    focused = any(tab.get("is_active") and locals_[0] in tab.get("windows", []) for tab in tree[0]["tabs"])
    if focused != state["focused"]:
        request(directory, "focus", pane=state["pane"], focused=focused)


def detach_only(directory):
    """An authoritatively empty workspace can still have its local view open."""
    from .cli import EMPTY_SNAPSHOT, view_query
    from .utility_tabs import utility_tab
    snapshot = directory / "workspace.kitty-session"
    if owner(directory) is None or not snapshot.is_file() or snapshot.read_text() != EMPTY_SNAPSHOT:
        return False
    tree = windows(directory)
    if not all(utility_tab(tab) for tab in tree[0]["tabs"]):
        return False
    state = request(directory)["tab"]
    selected = [w for tab in tree[0]["tabs"] for w in tab["windows"] if w.get("user_vars", {}).get("kzw_local")]
    if (len(selected) != 1 or state is None or selected[0]["user_vars"]["kzw_local"] != state["pane"] or
            selected[0]["user_vars"]["kzw_local_owner"] != owner(directory)):
        raise ValueError("local-only view ownership changed; refusing detach")
    if (directory / "remote.json").exists():
        remote_client.ensure_current_revision(directory)
    sync_focus(directory)
    view_query(directory, "close-window", "--match", f"id:{selected[0]['id']}")
    return True


def close(directory, pane, window_id=None):
    """Only explicit, confirmed closes enter here; uncertain cleanup is retained."""
    from .cli import view_query
    if window_id is not None:
        matches = [w for item in windows(directory) for tab in item["tabs"] for w in tab["windows"]
                   if w["id"] == window_id]
        if (len(matches) != 1 or not local_window(matches[0]) or
                matches[0]["user_vars"]["kzw_local"] != pane or
                matches[0]["user_vars"]["kzw_local_owner"] != owner(directory)):
            raise ValueError("local close selection changed")
    request(directory, "close", pane=pane)
    if window_id is not None:
        view_query(directory, "close-window", "--match", f"id:{window_id}")


def attach(identity, pane):
    if not re.fullmatch(r"[0-9a-f]{32}", identity) or not PANE.fullmatch(pane):
        raise ValueError("invalid local attachment")
    try:
        reply = remote_client.call("", {"method": "local.tab.launch", "owner": identity, "pane": pane})
        source = reply["source"]
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("KZW_"):
                env.pop(key)
        for key in ("ZMX_SESSION", "ZMX_SESSION_PREFIX", "ZMX_DIR", "XDG_RUNTIME_DIR", "TMPDIR"):
            env.pop(key, None)
        if source["socket_selector"]:
            env[source["socket_selector"]] = source["socket_base"]
        # Never inherit -o shell=kzw-pane from a separate workspace instance.
        shell = env.get("SHELL", "/bin/sh")
        if Path(shell).name in ("kzw-pane", "kzw"):
            shell = "/bin/sh"
        command = [source["executable"], "attach", pane]
        if reply["create"]:
            command += [shell, "-l"]
        stop = threading.Event()
        thread = threading.Thread(target=monitor_title, args=(pane, env, stop, 2, source["executable"]), daemon=True)
        thread.start()
        try:
            subprocess.run(command, env=env, check=True)
        finally:
            stop.set()
            thread.join(timeout=11)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"kzw: local attachment stopped: {exc}", file=sys.stderr, flush=True)
    try:
        # The client may return just before zmx finishes reaping its shell.
        for delay in (0, 0.25, 0.5, 1):
            if delay:
                time.sleep(delay)
            result = remote_client.call("", {"method": "local.tab.exited", "owner": identity, "pane": pane})
            if result["tab"] is None:
                return 0  # Kitty closes this wrapper only after acknowledgement.
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"kzw: could not confirm local shell exit: {exc}", file=sys.stderr, flush=True)
    print("Local session disconnected or its exit could not be verified. "
          "Close this tab explicitly, or detach and reattach.", flush=True)
    while True:
        time.sleep(3600)


def monitor_title(pane, environment, stop, interval=2, executable=None):
    """Set only this view's title from its pinned local zmx foreground PTY."""
    from .titles import read_process_details
    while not stop.is_set():
        try:
            detail = read_process_details("", environment, executable).get(pane)
            name = detail["name"] if detail else "?"
        except (OSError, ValueError, subprocess.SubprocessError):
            name = "?"
        if not stop.is_set():
            sys.stdout.write("\x1b]2;" + name + "\x07")
            sys.stdout.flush()
        stop.wait(interval)


if __name__ == "__main__":
    attach(*sys.argv[1:])
