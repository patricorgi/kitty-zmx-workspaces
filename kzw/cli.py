import argparse
import fcntl
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

from .core import FOCUS_PREFIX, PANE, atomic_write, check_name, focused_pane, pane_map, rewrite_export, saved_focus
from .titles import start_title_monitor
from . import remote_client

EMPTY_SNAPSHOT = "# kzw-empty-workspace\n"


def state_dir(name):
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    path = root / "kzw" / check_name(name)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def query(socket, *args):
    return subprocess.run(["kitty", "@", "--to", f"unix:{socket}", *args],
                          check=True, capture_output=True, text=True, timeout=5).stdout


@contextmanager
def capture_lock(directory):
    with open(directory / "capture.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def capture(directory, executable, allow_removals=False):
    with capture_lock(directory):
        return capture_locked(directory, executable, allow_removals)


def capture_locked(directory, executable, allow_removals=False, expected_panes=None, confirmed_exits=False):
    socket = directory / "kitty.sock"
    before_tree = json.loads(query(socket, "ls"))
    before = pane_map(before_tree)
    focus = focused_pane(before_tree)
    export = query(socket, "ls", "--output-format=session")
    after_tree = json.loads(query(socket, "ls"))
    after = pane_map(after_tree)
    if before != after or focus != focused_pane(after_tree):
        raise ValueError("panes or focus changed while taking snapshot")
    if expected_panes is not None and after != expected_panes:
        raise ValueError("unexpected pane change while saving a close; refusing cleanup")
    snapshot = FOCUS_PREFIX + json.dumps({"pane": focus}, separators=(",", ":")) + "\n"
    snapshot += rewrite_export(export, after, executable)
    # Layout itself can change without the window IDs changing.
    if query(socket, "ls", "--output-format=session") != export:
        raise ValueError("layout changed while taking snapshot")
    target = directory / "workspace.kitty-session"
    metadata = directory / "panes.json"
    if metadata.exists() and not allow_removals:
        old = set(json.loads(metadata.read_text()))
        current = {pane for pane, _ in after.values()}
        if not old.issubset(current):
            raise ValueError("pane removal detected; keeping last complete snapshot (use save --allow-removals)")
    if (directory / "remote.json").exists():
        if allow_removals and not confirmed_exits:
            raise ValueError("remote workspace removal is not yet supported; no snapshot was saved")
        remote_client.publish(directory, snapshot, [pane for pane, _ in after.values()], confirmed_exits=confirmed_exits)
    # IDs are also embedded in the session, so a failed metadata write cannot
    # cause a partially written Kitty session.
    atomic_write(target, snapshot)
    atomic_write(metadata, json.dumps(sorted(pane for pane, _ in after.values())) + "\n")
    return len(after)


def restore_focus(socket, pane):
    tree = json.loads(query(socket, "ls"))
    panes = pane_map(tree)
    matches = [window_id for window_id, (name, _) in panes.items() if name == pane]
    if len(matches) != 1:
        raise ValueError("saved focused pane is missing or ambiguous; refusing to change focus")
    query(socket, "focus-window", "--match", f"id:{matches[0]}")
    if focused_pane(json.loads(query(socket, "ls"))) != pane:
        raise ValueError("Kitty did not restore the saved focused pane")


def detach(name):
    directory = state_dir(name)
    if (directory / "pending-close.json").exists():
        raise ValueError("a pane close is incomplete; run kzw retry-close first")
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    # Serialize with the supervisor so it cannot overwrite the final snapshot
    # while the windows are being torn down. Failure to save means no close.
    with capture_lock(directory):
        for attempt in range(10):
            try:
                count = capture_locked(directory, executable)
                break
            except ValueError as exc:
                if str(exc) not in ("panes or focus changed while taking snapshot", "layout changed while taking snapshot") or attempt == 9:
                    raise
                time.sleep(0.1)
        query(directory / "kitty.sock", "close-window", "--match", "all")
    print(f"Detached {name!r}; saved focus and {count} panes")


def zmx_command(host, *args):
    if host:
        # Host and pane are validated by pane_map; no shell input is accepted.
        import shlex
        remote = 'PATH="$HOME/.local/bin:$PATH"; export PATH; unset ZMX_SESSION ZMX_SESSION_PREFIX; exec ' + shlex.join(["zmx", *args])
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote]
    else:
        cmd = ["zmx", *args]
    env = os.environ.copy()
    env.pop("ZMX_SESSION", None)
    env.pop("ZMX_SESSION_PREFIX", None)
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20, env=env).stdout


def exited_windows(tree):
    """Only panes whose attachment explicitly ended need verification."""
    panes = pane_map(tree)
    result = {}
    for os_window in tree:
        for tab in os_window.get("tabs", []):
            for window in tab.get("windows", []):
                if window.get("user_vars", {}).get("kzw_attach_ended") == "1":
                    result[window["id"]] = panes[window["id"]]
    return result


def synchronize_exits(directory, executable):
    """Remove only ended attachments whose host confirms the session is absent."""
    socket = directory / "kitty.sock"
    tree = json.loads(query(socket, "ls"))
    candidates = exited_windows(tree)
    if not candidates:
        return 0
    live_by_host = {}
    for _, host in candidates.values():
        if host not in live_by_host:
            try:
                live_by_host[host] = set(zmx_command(host, "list", "--short").splitlines())
            except (OSError, subprocess.SubprocessError):
                # Unknown is not exited. Do not guess when the network is down.
                continue
    gone = {wid: value for wid, value in candidates.items()
            if value[1] in live_by_host and value[0] not in live_by_host[value[1]]}
    if not gone:
        return 0
    with capture_lock(directory):
        if (directory / "pending-close.json").exists():
            return 0
        current_tree = json.loads(query(socket, "ls"))
        current = pane_map(current_tree)
        ended = exited_windows(current_tree)
        # Don't act on a replaced window or lose unverified disappeared panes.
        if any(current.get(wid) != value or ended.get(wid) != value for wid, value in gone.items()):
            return 0
        metadata = directory / "panes.json"
        if metadata.exists():
            old = set(json.loads(metadata.read_text()))
            if not old.issubset({pane for pane, _ in current.values()}):
                raise ValueError("unverified pane removal; refusing exit synchronization")
        expected = {wid: value for wid, value in current.items() if wid not in gone}
        if not expected:
            # Commit the explicit empty state before closing the last view.
            if (directory / "remote.json").exists():
                remote_client.publish(directory, EMPTY_SNAPSHOT, [], confirmed_exits=True)
            atomic_write(directory / "workspace.kitty-session", EMPTY_SNAPSHOT)
            atomic_write(metadata, "[]\n")
        for wid in gone:
            query(socket, "close-window", "--match", f"id:{wid}")
        if expected:
            for _ in range(50):
                try:
                    remaining = pane_map(json.loads(query(socket, "ls")))
                    if remaining == expected:
                        capture_locked(directory, executable, allow_removals=True, expected_panes=expected,
                                       confirmed_exits=True)
                        break
                except (ValueError, subprocess.SubprocessError):
                    pass
                time.sleep(0.1)
            else:
                raise ValueError("could not save layout after confirmed shell exits")
    return len(gone)


def cleanup_close_locked(directory):
    path = directory / "pending-close.json"
    data = json.loads(path.read_text())
    if data.get("phase") != "layout-saved":
        raise ValueError("close did not complete layout capture; inspect workspace before recovery")
    # Support records made by earlier versions, and validate all entries
    # before terminating anything. Persist progress after each session.
    entries = data.get("panes")
    if entries is None and "pane" in data and "host" in data:
        entries = [{"pane": data["pane"], "host": data["host"]}]
    if not isinstance(entries, list) or not entries:
        raise ValueError("invalid pending close record")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid pending close record")
        pane, host = entry.get("pane"), entry.get("host")
        if not isinstance(pane, str) or not PANE.fullmatch(pane) or not isinstance(host, str) or (host and (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host))) or pane in seen:
            raise ValueError("invalid pending close record")
        seen.add(pane)
    closed = []
    while entries:
        pane, host = entries[0]["pane"], entries[0]["host"]
        live = zmx_command(host, "list", "--short").splitlines()
        if pane in live:
            zmx_command(host, "kill", pane)
            if pane in zmx_command(host, "list", "--short").splitlines():
                raise ValueError("zmx session still exists after kill")
        closed.append(pane)
        entries = entries[1:]
        if entries:
            atomic_write(path, json.dumps({"phase": "layout-saved", "panes": entries}) + "\n")
    path.unlink()
    return closed


def close_pane(name, window_id):
    return close_target(name, "pane", window_id)


def close_tab(name, tab_id):
    return close_target(name, "tab", tab_id)


def close_target(name, kind, target_id):
    directory = state_dir(name)
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    with capture_lock(directory):
        pending = directory / "pending-close.json"
        if pending.exists():
            raise ValueError("a pane close is incomplete; run kzw retry-close first")
        if (directory / "remote.json").exists():
            raise ValueError("managed closure of remote-authoritative workspaces is not yet supported")
        # Save before closing so a failed operation never discards the last view.
        capture_locked(directory, executable)
        socket = directory / "kitty.sock"
        tree = json.loads(query(socket, "ls"))
        panes = pane_map(tree)
        if kind == "pane":
            if target_id not in panes:
                raise ValueError("window ID is not a managed pane in this workspace")
            selected = {target_id}
            command = "close-window"
        elif kind == "tab":
            tabs = [t for os_window in tree for t in os_window.get("tabs", []) if t.get("id") == target_id]
            if len(tabs) != 1:
                raise ValueError("tab ID is not a unique tab in this workspace")
            selected = {window["id"] for window in tabs[0].get("windows", [])}
            if not selected or not selected.issubset(panes):
                raise ValueError("tab contains an unmanaged or missing pane")
            command = "close-tab"
        else:
            raise ValueError("invalid close target")
        expected = set(panes) - selected
        if not expected:
            raise ValueError("refusing to close the last pane; detach the workspace instead")
        record = {"phase": "closing", "panes": [{"pane": panes[i][0], "host": panes[i][1]} for i in sorted(selected)]}
        atomic_write(pending, json.dumps(record) + "\n")
        query(socket, command, "--match", f"id:{target_id}")
        # Kitty closes asynchronously. Check identity as well as count, and
        # never interpret an unrelated close as permission to destroy it.
        for _ in range(50):
            remaining = pane_map(json.loads(query(socket, "ls")))
            if set(remaining) == expected and all(remaining[k] == panes[k] for k in expected):
                break
            time.sleep(0.1)
        else:
            raise ValueError("pane removal could not be verified; close record retained")
        capture_locked(directory, executable, allow_removals=True,
                       expected_panes={key: panes[key] for key in expected})
        record["phase"] = "layout-saved"
        atomic_write(pending, json.dumps(record) + "\n")
        closed = cleanup_close_locked(directory)
    print(f"Closed {kind} and {len(closed)} zmx session(s); saved the reduced layout")


def retry_close(name):
    directory = state_dir(name)
    with capture_lock(directory):
        if not (directory / "pending-close.json").exists():
            raise ValueError("no pending close")
        panes = cleanup_close_locked(directory)
    print(f"Finished cleanup of {len(panes)} zmx session(s)")


def run(name, host, remote_attached=False):
    directory = state_dir(name)
    if (directory / "remote.json").exists() and not remote_attached:
        raise ValueError("remote workspace: use kzw attach NAME --host HOST")
    pane_executable = shutil.which("kzw-pane")
    if not pane_executable:
        raise ValueError("kzw-pane not found; install the project and activate its environment")
    if not shutil.which("kitty"):
        raise ValueError("kitty not found on PATH")
    if not host and not shutil.which("zmx"):
        raise ValueError("zmx not found on PATH; install it before starting a local workspace")
    with open(directory / "lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace is already running") from None
        if (directory / "pending-close.json").exists():
            raise ValueError("a pane close is incomplete; inspect it or run kzw retry-close")
        sock = directory / "kitty.sock"
        # The lock proves no kzw supervisor is using this workspace. Refuse a
        # stale socket rather than unlinking anything we cannot verify is ours.
        if sock.exists():
            raise ValueError(f"socket already exists: {sock}; check for a running Kitty before removing it")
        snapshot = directory / "workspace.kitty-session"
        if snapshot.exists() and snapshot.read_text() == EMPTY_SNAPSHOT:
            print(f"Workspace {name!r} has no live panes")
            return 0
        env = os.environ.copy()
        env["KZW_HOST"] = host
        env["KZW_CHANGE_SIGNAL"] = str(directory / "watcher.changed")
        args = ["kitty", "--listen-on", f"unix:{sock}", "-o", "allow_remote_control=socket-only",
                "-o", f"shell={pane_executable}", "-o", "enabled_layouts=splits,tall,stack",
                "-o", f"watcher={Path(__file__).with_name('watcher.py')}"]
        focus = saved_focus(snapshot.read_text()) if snapshot.exists() else None
        if snapshot.exists():
            args += ["--session", str(snapshot)]
        else:
            args += ["--session", "none"]
        child = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL)
        title_stop = start_title_monitor(sock, query)
        print(f"Workspace {name!r} started. Keep this supervisor running; close Kitty to detach.", flush=True)
        last_error = None
        pending_focus = focus
        change_signal = directory / "watcher.changed"
        last_signal = None
        next_capture = 0.0
        next_exit_check = 0.0
        try:
            while child.poll() is None:
                try:
                    if pending_focus:
                        restore_focus(sock, pending_focus)
                        pending_focus = None
                    if time.monotonic() >= next_exit_check:
                        synchronize_exits(directory, pane_executable)
                        next_exit_check = time.monotonic() + 1
                    try:
                        signal = change_signal.stat().st_mtime_ns
                    except FileNotFoundError:
                        signal = None
                    if signal != last_signal or time.monotonic() >= next_capture:
                        capture(directory, pane_executable)
                        last_signal = signal
                        next_capture = time.monotonic() + 1
                    last_error = None
                except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                    message = str(exc)
                    if message != last_error:
                        print(f"kzw: snapshot deferred: {message}", file=sys.stderr, flush=True)
                        last_error = message
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nkzw: supervisor stopped; Kitty remains open, but snapshots have stopped.", file=sys.stderr)
            # Do not release the lock while Kitty still owns the workspace.
            child.wait()
        finally:
            title_stop.set()
    return child.returncode


def create_remote(name, host):
    directory = state_dir(name)
    with open(directory / "lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace is already running") from None
        remote_client.create(directory, name, host)
    print(f"Created dormant remote workspace {name!r} on {host}; use kzw attach to open it")


def attach_remote(name, host):
    directory = state_dir(name)
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    with open(directory / "lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace is already running") from None
        if (directory / "kitty.sock").exists() or (directory / "pending-close.json").exists():
            raise ValueError("local workspace may still be active or has a pending close")
        remote_client.pull(directory, name, host, executable)
    return run(name, host, remote_attached=True)


def main():
    parser = argparse.ArgumentParser(prog="kzw")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="open a workspace and supervise snapshots")
    start.add_argument("name")
    start.add_argument("--host", default="", help="SSH host for newly created panes")
    create_parser = commands.add_parser("create", help="create a dormant remote workspace")
    create_parser.add_argument("name")
    create_parser.add_argument("--host", required=True)
    attach_parser = commands.add_parser("attach", help="attach a remote workspace")
    attach_parser.add_argument("name")
    attach_parser.add_argument("--host", required=True)
    save = commands.add_parser("save", help="save a live workspace explicitly")
    save.add_argument("name")
    save.add_argument("--allow-removals", action="store_true")
    det = commands.add_parser("detach", help="save the current focus and close the workspace view")
    det.add_argument("name")
    close = commands.add_parser("close-pane", help="explicitly close a Kitty pane and its zmx session")
    close.add_argument("name")
    close.add_argument("--window-id", type=int, required=True)
    close_tab_parser = commands.add_parser("close-tab", help="explicitly close a Kitty tab and all of its zmx sessions")
    close_tab_parser.add_argument("name")
    close_tab_parser.add_argument("--tab-id", type=int, required=True)
    retry = commands.add_parser("retry-close", help="retry cleanup after a saved pane close")
    retry.add_argument("name")
    list_parser = commands.add_parser("list", help="list saved workspaces")
    list_parser.add_argument("--host", default="")
    args = parser.parse_args()
    try:
        if args.command == "start":
            return run(args.name, args.host)
        if args.command == "create":
            create_remote(args.name, args.host)
            return 0
        if args.command == "attach":
            return attach_remote(args.name, args.host)
        if args.command == "save":
            executable = shutil.which("kzw-pane")
            if not executable:
                raise ValueError("kzw-pane not found on PATH")
            print(f"Saved {capture(state_dir(args.name), executable, args.allow_removals)} panes")
            return 0
        if args.command == "detach":
            detach(args.name)
            return 0
        if args.command == "close-pane":
            close_pane(args.name, args.window_id)
            return 0
        if args.command == "close-tab":
            close_tab(args.name, args.tab_id)
            return 0
        if args.command == "retry-close":
            retry_close(args.name)
            return 0
        if args.host:
            for workspace in remote_client.call(args.host, {"method": "workspace.list"})["workspaces"]:
                print(workspace["name"])
            return 0
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kzw"
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if (path / "workspace.kitty-session").is_file():
                    print(path.name)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"kzw: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
