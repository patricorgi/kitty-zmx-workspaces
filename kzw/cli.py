import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext

from .core import FOCUS_PREFIX, PANE, atomic_write, check_name, focused_pane, pane_map, rewrite_export, saved_focus
from .titles import start_title_monitor
from . import remote_client
from . import supervisor_window

EMPTY_SNAPSHOT = "# kzw-empty-workspace\n"


def log_pane_event(event, panes):
    """Print one lifecycle event using the stable session IDs without kzw-."""
    if event not in ("pane_created", "pane_destroyed", "tab_created", "tab_destroyed"):
        raise ValueError("invalid lifecycle event")
    panes = list(panes)
    if not panes or any(not isinstance(p, str) or not PANE.fullmatch(p) for p in panes) or len(set(panes)) != len(panes):
        raise ValueError("invalid lifecycle event pane identity")
    panes.sort()
    print(f"{event}:{','.join(pane[4:] for pane in panes)}", flush=True)


def state_dir(name, origin=""):
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    state_root = root / "kzw"
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_root / check_name(name)
    if origin:
        if origin.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", origin):
            raise ValueError("invalid SSH host")
        host_key = hashlib.sha256(origin.encode()).hexdigest()[:24]
        scoped = root / "kzw" / ".remote" / host_key / name
        legacy = path / "remote.json"
        # Keep an existing remote cache in place, including pending operations.
        # Never infer that an ordinary local workspace belongs to this host.
        if legacy.is_file():
            config = json.loads(legacy.read_text())
            if config.get("host") == origin and config.get("workspace") == name:
                if scoped.exists():
                    raise ValueError("both legacy and host-scoped remote caches exist; inspect them before proceeding")
                return path
        path = scoped
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def query(socket, *args):
    def raw(*command):
        return subprocess.run(["kitty", "@", "--to", f"unix:{socket}", *command],
                              check=True, capture_output=True, text=True, timeout=5).stdout

    if args not in (("ls",), ("ls", "--output-format=session")):
        return raw(*args)
    tree = json.loads(raw("ls"))
    matches = []
    for os_window in tree:
        managed = [w for tab in os_window.get("tabs", []) for w in tab.get("windows", [])
                   if isinstance(w.get("user_vars", {}).get("kzw_pane"), str) and
                   PANE.fullmatch(w["user_vars"]["kzw_pane"])]
        if managed:
            matches.append((os_window, managed))
    if len(matches) > 1:
        raise ValueError("managed panes span multiple Kitty OS windows; refusing an ambiguous snapshot")
    if not matches:
        if args == ("ls",):
            return "[]"
        raise ValueError("workspace Kitty OS window is not ready")
    os_window, managed = matches[0]
    # The workspace remains the last focused workspace window even when a
    # separate utility OS window is currently focused.
    os_window["last_focused"] = True
    if args == ("ls",):
        return json.dumps([os_window])
    pane_map([os_window])  # refuse unmanaged panes inside the workspace
    anchor = int(managed[0]["id"])
    os_id = int(os_window["id"])
    kitten = Path(__file__).with_name("scoped_session_kitten.py")
    response = json.loads(raw("kitten", "--match", f"id:{anchor}", str(kitten), str(os_id)))
    if not isinstance(response, dict) or response.get("os_window_id") != os_id or not isinstance(response.get("session"), str):
        raise ValueError("invalid scoped Kitty session response")
    return response["session"]


@contextmanager
def capture_lock(directory):
    with open(directory / "capture.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def capture(directory, executable, allow_removals=False):
    with capture_lock(directory):
        if (directory / "pending-create.json").exists():
            raise ValueError("a pane creation is incomplete; snapshot deferred")
        if (directory / "pending-close.json").exists():
            raise ValueError("a pane close is incomplete; snapshot deferred")
        return capture_locked(directory, executable, allow_removals)


def native_journals(directory, panes):
    """Return only matching, unmodified journals for these exact identities."""
    root = directory / "native-pending"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("unsafe native creation journal directory")
    if root.exists() and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise ValueError("unsafe native creation journal directory")
    records = []
    for pane, host in panes:
        path = root / (pane + ".json")
        if path.is_symlink():
            raise ValueError("unsafe native creation journal")
        if not path.exists():
            continue
        text = path.read_text()
        if json.loads(text) != {"pane": pane, "host": host, "workspace": directory.name}:
            raise ValueError("native creation journal conflicts with pane identity")
        records.append((path, text))
    return records


def remove_native_journals(records):
    for path, text in records:
        if not path.is_symlink() and path.is_file() and path.read_text() == text:
            ready = path.with_suffix(".ready")
            expected = hashlib.sha256(text.encode()).hexdigest() + "\n"
            if not ready.is_symlink() and ready.is_file() and ready.read_text() == expected:
                ready.unlink()
            path.unlink()


def check_native_journals(directory):
    root = directory / "native-pending"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("unsafe native creation journal directory")
    if root.exists() and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise ValueError("unsafe native creation journal directory")
    if not root.exists():
        return
    metadata = directory / "panes.json"
    saved = set(json.loads(metadata.read_text())) if metadata.is_file() else set()
    for path in root.iterdir():
        if path.name.endswith(".ready") and not path.is_symlink() and path.is_file():
            journal = path.with_suffix(".json")
            if journal.is_file() and not journal.is_symlink():
                continue
        if path.is_symlink() or not path.is_file() or not path.name.endswith(".json"):
            raise ValueError("unrecognized native creation journal; inspect it manually")
        pane = path.name[:-5]
        if not PANE.fullmatch(pane):
            raise ValueError("invalid native creation journal; inspect it manually")
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or record.get("pane") != pane or record.get("workspace") != directory.name or
                not isinstance(record.get("host"), str)):
            raise ValueError("invalid native creation journal; inspect it manually")
        if pane not in saved:
            raise ValueError(f"native pane creation {pane} is incomplete; inspect its journal before restoring")


def stage_native_layout(directory, snapshot, old, current, journals):
    added = current - old
    by_pane = {path.stem: (path, text) for path, text in journals}
    if not added or not added.issubset(by_pane):
        return
    target = directory / "workspace.kitty-session"
    if old and not target.is_file():
        raise ValueError("cannot stage native creation without the complete saved layout")
    stage = directory / "native-stage.json"
    if stage.is_symlink():
        raise ValueError("unsafe native layout stage")
    if stage.exists():
        previous = json.loads(stage.read_text())
        if not isinstance(previous, dict) or previous.get("kind") != "kzw-native-stage-v1":
            raise ValueError("unrecognized native layout stage")
    baseline = target.read_text() if target.is_file() else ""
    staged = {"kind": "kzw-native-stage-v1", "base_hash": hashlib.sha256(baseline.encode()).hexdigest(),
              "snapshot": snapshot, "panes": sorted(current), "added": sorted(added)}
    if (directory / "remote.json").exists():
        remote_client.stage_layout(directory, snapshot, sorted(current))
    atomic_write(stage, json.dumps(staged, separators=(",", ":")) + "\n")
    for pane in sorted(added):
        path, text = by_pane[pane]
        ready = path.with_suffix(".ready")
        expected = hashlib.sha256(text.encode()).hexdigest() + "\n"
        try:
            fd = os.open(ready, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if ready.is_symlink() or not ready.is_file() or ready.read_text() != expected:
                raise ValueError("native creation acknowledgement conflicts with existing state") from None
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(expected)
                stream.flush()
                os.fsync(stream.fileno())


def recover_local_native_stage(directory, executable):
    """Promote an interrupted stage only when all its exact sessions exist."""
    stage = directory / "native-stage.json"
    if stage.is_symlink():
        raise ValueError("unsafe native layout stage")
    if not stage.exists():
        return False
    if (directory / "remote.json").exists():
        return False
    data = json.loads(stage.read_text())
    if not isinstance(data, dict) or data.get("kind") != "kzw-native-stage-v1":
        raise ValueError("unrecognized native layout stage")
    snapshot, panes, added = data.get("snapshot"), data.get("panes"), data.get("added")
    if (not isinstance(snapshot, str) or not isinstance(panes, list) or not isinstance(added, list) or
            not panes or not added or
            any(not isinstance(p, str) or not PANE.fullmatch(p) for p in panes + added) or
            len(panes) != len(set(panes)) or len(added) != len(set(added)) or not set(added).issubset(panes)):
        raise ValueError("invalid native layout stage")
    target = directory / "workspace.kitty-session"
    if target.is_symlink() or not target.is_file():
        raise ValueError("native layout stage has no safe baseline")
    baseline = target.read_text()
    if baseline != snapshot and hashlib.sha256(baseline.encode()).hexdigest() != data.get("base_hash"):
        raise ValueError("native layout stage baseline changed")
    found = {}
    for line in snapshot.splitlines():
        if not line.startswith("launch "):
            continue
        args = shlex.split(line)
        if (len(args) < 8 or not args[1].startswith("kitty-unserialize-data=") or
                args[-6] != executable or args[-5] != "--pane" or args[-3] != "--host" or
                args[-1] != "--existing" or args[-4] in found):
            raise ValueError("invalid launch in native layout stage")
        found[args[-4]] = args[-2]
    if set(found) != set(panes):
        raise ValueError("native layout stage manifest differs from its launches")
    if any(host and (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host)) for host in found.values()):
        raise ValueError("invalid host in native layout stage")
    journals = native_journals(directory, [(p, found[p]) for p in added])
    metadata = directory / "panes.json"
    committed = (baseline == snapshot and metadata.is_file() and not metadata.is_symlink() and
                 json.loads(metadata.read_text()) == sorted(panes))
    if not committed and {p.stem for p, _ in journals} != set(added):
        raise ValueError("native layout stage is missing creation journals")
    by_host = {}
    for pane, host in found.items():
        if host not in by_host:
            by_host[host] = set(zmx_command(host, "list", "--short").splitlines())
        if pane not in by_host[host]:
            raise ValueError("native staged session is missing; refusing to recreate it")
    atomic_write(target, snapshot)
    atomic_write(directory / "panes.json", json.dumps(sorted(panes)) + "\n")
    remove_native_journals(journals)
    stage.unlink()
    return True


def recover_local_managed_creation(directory, executable):
    """Finish a detached managed split only from its exact durable stage."""
    pending = directory / "pending-create.json"
    stage = directory / "native-stage.json"
    if not pending.exists():
        return False
    if pending.is_symlink() or stage.is_symlink() or (directory / "remote.json").exists():
        raise ValueError("managed creation recovery has unsafe or remote state")
    text = pending.read_text()
    record = json.loads(text)
    if not stage.exists():
        if record.get("phase") != "stage-recovery":
            return False
        snapshot, metadata = directory / "workspace.kitty-session", directory / "panes.json"
        pane, host = record.get("pane"), record.get("host")
        if (snapshot.is_symlink() or not snapshot.is_file() or metadata.is_symlink() or not metadata.is_file() or
                hashlib.sha256(snapshot.read_bytes()).hexdigest() != record.get("stage_hash") or
                pane not in json.loads(metadata.read_text()) or not isinstance(host, str) or
                not isinstance(pane, str) or not PANE.fullmatch(pane) or
                (host and (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host))) or
                pane not in zmx_command(host, "list", "--short").splitlines()):
            raise ValueError("managed creation recovery acknowledgement is inconsistent")
        if pending.read_text() != text:
            raise ValueError("managed creation journal changed during recovery")
        pending.unlink()
        return True
    data = json.loads(stage.read_text())
    pane, host, target, operation = (record.get(k) for k in ("pane", "host", "target", "request_id"))
    if (record.get("kind") not in ("right", "below", "split", "tab") or
            not isinstance(pane, str) or not PANE.fullmatch(pane) or
            not isinstance(target, str) or not PANE.fullmatch(target) or target == pane or
            not isinstance(host, str) or not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation) or
            not isinstance(data, dict) or data.get("kind") != "kzw-native-stage-v1" or
            pane not in data.get("added", []) or target not in data.get("panes", [])):
        raise ValueError("managed creation journal does not match its staged layout")
    journal = directory / "native-pending" / (pane + ".json")
    target_snapshot, metadata = directory / "workspace.kitty-session", directory / "panes.json"
    committed = (record.get("phase") == "stage-recovery" and target_snapshot.is_file() and
                 not target_snapshot.is_symlink() and target_snapshot.read_text() == data.get("snapshot") and
                 metadata.is_file() and not metadata.is_symlink() and
                 json.loads(metadata.read_text()) == sorted(data.get("panes", [])))
    if journal.is_symlink() or (not journal.is_file() and not committed) or (journal.is_file() and json.loads(journal.read_text()) != {
            "pane": pane, "host": host, "workspace": directory.name}):
        raise ValueError("managed creation journal does not match its staged pane")
    if not isinstance(data.get("snapshot"), str):
        raise ValueError("managed creation stage has no snapshot")
    stage_hash = hashlib.sha256(data["snapshot"].encode()).hexdigest()
    if record.get("phase") == "stage-recovery" and record.get("stage_hash") != stage_hash:
        raise ValueError("managed creation stage changed during recovery")
    record.update(phase="stage-recovery", stage_hash=stage_hash)
    text = json.dumps(record) + "\n"
    atomic_write(pending, text)
    recover_local_native_stage(directory, executable)
    if pending.is_symlink() or not pending.is_file() or pending.read_text() != text:
        raise ValueError("managed creation journal changed during recovery")
    pending.unlink()
    return True


def recover_remote_managed_creation(directory, name, host, executable):
    """Recover a detached managed split using the origin's staged layout."""
    pending = directory / "pending-create.json"
    if pending.is_symlink() or not pending.is_file():
        raise ValueError("managed creation journal is unavailable")
    record = json.loads(pending.read_text())
    pane, target, operation = (record.get(k) for k in ("pane", "target", "request_id"))
    if (record.get("kind") not in ("right", "below", "split", "tab") or record.get("host") != host or
            not isinstance(pane, str) or not PANE.fullmatch(pane) or
            not isinstance(target, str) or not PANE.fullmatch(target) or pane == target or
            not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation)):
        raise ValueError("invalid managed creation journal")
    remote = remote_client.call(host, {"method": "workspace.get", "workspace": name})
    staged = remote.get("staged")
    if staged:
        portable = staged.get("snapshot")
        if not isinstance(portable, str):
            raise ValueError("invalid remote staged layout")
        _, panes = remote_client.translate(portable, executable, host, False)
        if (staged.get("revision") != remote.get("revision") or panes != sorted(staged.get("panes", [])) or
                pane not in remote.get("reservations", []) or target not in remote.get("panes", []) or
                pane not in panes):
            raise ValueError("remote staged layout does not match managed creation")
        digest = remote_client.snapshot_hash(portable, panes)
        if record.get("phase") == "stage-recovery" and record.get("stage_hash") != digest:
            raise ValueError("remote staged layout changed during recovery")
        record.update(phase="stage-recovery", stage_hash=digest)
        text = json.dumps(record) + "\n"
        atomic_write(pending, text)
    else:
        text = pending.read_text()
        if record.get("phase") != "stage-recovery":
            raise ValueError("remote managed creation has no staged layout")
        portable = remote.get("snapshot")
        if not isinstance(portable, str):
            raise ValueError("remote recovery snapshot is unavailable")
        _, panes = remote_client.translate(portable, executable, host, False)
        if (pane not in panes or target not in panes or remote.get("reservations") or
                remote_client.snapshot_hash(portable, panes) != record.get("stage_hash")):
            raise ValueError("remote recovered layout does not match its journal")
    remote_client.pull(directory, name, host, executable)
    status = remote_client.status(directory)
    if status.get("missing") or set(status.get("live", [])) != set(panes):
        raise ValueError("remote recovered sessions are not all confirmed live")
    if pending.is_symlink() or not pending.is_file() or pending.read_text() != text:
        raise ValueError("managed creation journal changed during recovery")
    pending.unlink()
    return True


def capture_locked(directory, executable, allow_removals=False, expected_panes=None, confirmed_exits=False, confirmed_close=False, close_panes=None):
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
    old = set(json.loads(metadata.read_text())) if metadata.exists() else set()
    current = {pane for pane, _ in after.values()}
    journals = native_journals(directory, after.values())
    if not allow_removals:
        if not old.issubset(current):
            raise ValueError("pane removal detected; keeping last complete snapshot (use save --allow-removals)")
    if journals and not allow_removals:
        stage_native_layout(directory, snapshot, old, current, journals)
    if not (directory / "remote.json").exists():
        added = {value for value in after.values() if value[0] not in old}
        live_by_host = {}
        for pane, host in added:
            if host not in live_by_host:
                live_by_host[host] = set(zmx_command(host, "list", "--short").splitlines())
            if pane not in live_by_host[host]:
                raise ValueError("new pane session is missing or not yet ready")
        if added and (pane_map(json.loads(query(socket, "ls"))) != after or
                      query(socket, "ls", "--output-format=session") != export):
            raise ValueError("layout changed while taking snapshot")
    if (directory / "remote.json").exists():
        if allow_removals and not (confirmed_exits or confirmed_close):
            raise ValueError("remote workspace removal is not yet supported; no snapshot was saved")
        kwargs = {"confirmed_exits": confirmed_exits}
        if confirmed_close:
            kwargs.update(confirmed_close=True, close_panes=close_panes)
        remote_client.publish(directory, snapshot, [pane for pane, _ in after.values()], **kwargs)
    # IDs are also embedded in the session, so a failed metadata write cannot
    # cause a partially written Kitty session.
    atomic_write(target, snapshot)
    atomic_write(metadata, json.dumps(sorted(pane for pane, _ in after.values())) + "\n")
    remove_native_journals(journals)
    stage = directory / "native-stage.json"
    if stage.is_file() and not stage.is_symlink():
        staged = json.loads(stage.read_text())
        if (isinstance(staged, dict) and staged.get("kind") == "kzw-native-stage-v1" and
                set(staged.get("panes", [])) == current and staged.get("snapshot") == snapshot):
            stage.unlink()
    return len(after)


def capture_stable_locked(directory, executable):
    """Allow transient Kitty overlay/layout transitions, never incomplete state."""
    transient = {"panes or focus changed while taking snapshot", "layout changed while taking snapshot",
                 "a Kitty pane is not managed by kzw; refusing to save an incomplete workspace",
                 "focused pane is not managed by kzw", "layout changed during snapshot; retrying",
                 "layout export did not include every managed pane"}
    for attempt in range(30):
        try:
            return capture_locked(directory, executable)
        except ValueError as exc:
            if str(exc) not in transient or attempt == 29:
                raise
            time.sleep(0.1)


def restore_focus(socket, pane):
    tree = json.loads(query(socket, "ls"))
    panes = pane_map(tree)
    matches = [window_id for window_id, (name, _) in panes.items() if name == pane]
    if len(matches) != 1:
        raise ValueError("saved focused pane is missing or ambiguous; refusing to change focus")
    query(socket, "focus-window", "--match", f"id:{matches[0]}")
    if focused_pane(json.loads(query(socket, "ls"))) != pane:
        raise ValueError("Kitty did not restore the saved focused pane")


def detach(name, origin=""):
    directory = state_dir(name, origin) if origin else state_dir(name)
    if (directory / "pending-close.json").exists():
        raise ValueError("a pane close is incomplete; run kzw retry-close first")
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    # Serialize with the supervisor so it cannot overwrite the final snapshot
    # while the windows are being torn down. Failure to save means no close.
    with capture_lock(directory):
        if (directory / "pending-create.json").exists():
            raise ValueError("a pane creation is incomplete; inspect pending-create.json")
        count = capture_stable_locked(directory, executable)
        if (directory / "remote.json").exists():
            remote_client.ensure_current_revision(directory)
        tree = json.loads(query(directory / "kitty.sock", "ls"))
        mapped = pane_map(tree)
        if {pane for pane, _ in mapped.values()} != set(json.loads((directory / "panes.json").read_text())):
            raise ValueError("workspace changed after final snapshot; refusing to detach")
        ids = sorted(mapped)
        query(directory / "kitty.sock", "close-window", "--match", " or ".join(f"id:{wid}" for wid in ids))
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


def local_empty_retirable(directory):
    """Recognize only an empty workspace without any uncertain operations."""
    if (directory / "remote.json").exists():
        return False
    for name in ("pending-create.json", "pending-close.json", "native-stage.json", "bootstrap.kitty-session"):
        path = directory / name
        if path.exists() or path.is_symlink():
            return False
    pending = directory / "native-pending"
    if pending.is_symlink() or (pending.exists() and (not pending.is_dir() or any(pending.iterdir()))):
        return False
    snapshot, metadata = directory / "workspace.kitty-session", directory / "panes.json"
    if (snapshot.is_symlink() or metadata.is_symlink() or not snapshot.is_file() or not metadata.is_file() or
            snapshot.read_text() != EMPTY_SNAPSHOT or json.loads(metadata.read_text()) != []):
        return False
    return True


def retire_local_empty(directory):
    """Forget only a fully reconciled local empty workspace, never its directory."""
    if not local_empty_retirable(directory):
        return False
    snapshot, metadata = directory / "workspace.kitty-session", directory / "panes.json"
    # No broad removal: locks, logs and any unrecognized files remain untouched.
    snapshot.unlink()
    metadata.unlink()
    return True


def synchronize_exits(directory, executable, remote_hint=()):
    """Remove only panes whose host confirms the session is absent."""
    socket = directory / "kitty.sock"
    tree = json.loads(query(socket, "ls"))
    candidates = exited_windows(tree)
    # A wrapper can fail before its first attach (reservation, staging, or
    # authentication). Its absent session is not evidence of a shell exit.
    pending_root = directory / "native-pending"
    candidates = {wid: value for wid, value in candidates.items()
                  if not (pending_root / (value[0] + ".json")).exists()}
    hinted_ids = set()
    if remote_hint and (directory / "remote.json").exists():
        for wid, value in pane_map(tree).items():
            if value[0] in remote_hint:
                candidates[wid] = value
                hinted_ids.add(wid)
    if not candidates:
        return 0
    live_by_host = {}
    remote_missing = None
    if (directory / "remote.json").exists():
        # The origin decides which sessions belong to this workspace and
        # whether they are absent. Failed RPC/listing is unknown, not exited.
        remote_missing = set(remote_client.status(directory)["missing"])
    for _, host in candidates.values():
        if remote_missing is not None:
            continue
        if host not in live_by_host:
            try:
                live_by_host[host] = set(zmx_command(host, "list", "--short").splitlines())
            except (OSError, subprocess.SubprocessError):
                # Unknown is not exited. Do not guess when the network is down.
                continue
    gone = {wid: value for wid, value in candidates.items()
            if (value[0] in remote_missing if remote_missing is not None else
                value[1] in live_by_host and value[0] not in live_by_host[value[1]])}
    if not gone:
        return 0
    with capture_lock(directory):
        if (directory / "pending-close.json").exists() or (directory / "pending-create.json").exists():
            return 0
        if (directory / "remote.json").exists():
            remote_client.renew_lease(directory)
            remote_client.ensure_current_revision(directory)
        current_tree = json.loads(query(socket, "ls"))
        current = pane_map(current_tree)
        ended = exited_windows(current_tree)
        # Don't act on a replaced window or lose unverified disappeared panes.
        if any(current.get(wid) != value or (wid not in hinted_ids and ended.get(wid) != value)
               for wid, value in gone.items()):
            return 0
        metadata = directory / "panes.json"
        if metadata.exists():
            old = set(json.loads(metadata.read_text()))
            if not old.issubset({pane for pane, _ in current.values()}):
                raise ValueError("unverified pane removal; refusing exit synchronization")
        expected = {wid: value for wid, value in current.items() if wid not in gone}
        journals = native_journals(directory, gone.values())
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
        remove_native_journals(journals)
        if not expected:
            retire_local_empty(directory)
        for os_window in current_tree:
            for tab in os_window.get("tabs", []):
                ids = {window["id"] for window in tab.get("windows", [])}
                removed = ids & gone.keys()
                if removed and ids == removed:
                    log_pane_event("tab_destroyed", [gone[wid][0] for wid in removed])
                else:
                    for wid in sorted(removed):
                        log_pane_event("pane_destroyed", [gone[wid][0]])
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
    if data.get("remote"):
        if not (directory / "remote.json").exists():
            raise ValueError("remote close record has no remote configuration")
        remote_client.retry_cleanup(directory)
        path.unlink()
        return [entry["pane"] for entry in entries]
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
            data["panes"] = entries
            atomic_write(path, json.dumps(data) + "\n")
    path.unlink()
    if data.get("last"):
        retire_local_empty(directory)
    return closed


def close_pane(name, window_id, origin=""):
    return close_target(name, "pane", window_id, origin) if origin else close_target(name, "pane", window_id)


def close_tab(name, tab_id, origin=""):
    return close_target(name, "tab", tab_id, origin) if origin else close_target(name, "tab", tab_id)


def close_target(name, kind, target_id, origin="", confirmed_selection=None):
    directory = state_dir(name, origin) if origin else state_dir(name)
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    with capture_lock(directory):
        if (directory / "remote.json").exists():
            remote_client.renew_lease(directory)
        if (directory / "pending-create.json").exists():
            raise ValueError("a pane creation is incomplete; inspect pending-create.json")
        pending = directory / "pending-close.json"
        if pending.exists():
            raise ValueError("a pane close is incomplete; run kzw retry-close first")
        remote = (directory / "remote.json").exists()
        # Save before closing so a failed operation never discards the last view.
        capture_stable_locked(directory, executable)
        if remote:
            remote_client.ensure_current_revision(directory)
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
        if confirmed_selection is not None:
            actual = {str(i): list(panes[i]) for i in selected}
            if actual != confirmed_selection:
                raise ValueError("close target changed after confirmation; nothing closed")
        expected = set(panes) - selected
        record = {"phase": "closing", "remote": remote, "last": not expected,
                  "kind": kind, "target_id": target_id,
                  "panes": [{"pane": panes[i][0], "host": panes[i][1]} for i in sorted(selected)],
                  "before": {str(i): list(value) for i, value in panes.items()}}
        atomic_write(pending, json.dumps(record) + "\n")
        if not expected:
            # No Kitty query is possible once its last view has gone. Persist
            # the explicitly authorized empty state and cleanup intent first.
            if remote:
                remote_client.publish(directory, EMPTY_SNAPSHOT, [], confirmed_close=True,
                                      close_panes=[panes[i][0] for i in selected])
            atomic_write(directory / "workspace.kitty-session", EMPTY_SNAPSHOT)
            atomic_write(directory / "panes.json", "[]\n")
            record["phase"] = "layout-saved"
            atomic_write(pending, json.dumps(record) + "\n")
            query(socket, command, "--match", f"id:{target_id}")
            closed = cleanup_close_locked(directory)
            log_pane_event("tab_destroyed" if kind == "tab" else "pane_destroyed", closed)
            return
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
                       expected_panes={key: panes[key] for key in expected}, confirmed_close=remote,
                       close_panes=[panes[i][0] for i in selected] if remote else None)
        record["phase"] = "layout-saved"
        atomic_write(pending, json.dumps(record) + "\n")
        closed = cleanup_close_locked(directory)
    log_pane_event("tab_destroyed" if kind == "tab" else "pane_destroyed", closed)


def create_pane(name, kind, window_id, expected_pane, host, origin="", cwd_uri=""):
    """Serialize managed creation with closes and retain uncertain attempts."""
    if kind not in ("right", "below", "split", "tab") or not PANE.fullmatch(expected_pane):
        raise ValueError("invalid creation target")
    directory = state_dir(name, origin) if origin else state_dir(name)
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    cwd = ""
    if cwd_uri:
        try:
            from .shell_cwd import resolve
            cwd = resolve(cwd_uri, host)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"kzw: shell working directory unavailable; using default: {exc}", file=sys.stderr, flush=True)
    with capture_lock(directory):
        if (directory / "remote.json").exists():
            remote_client.renew_lease(directory)
        if (directory / "pending-close.json").exists() or (directory / "pending-create.json").exists():
            raise ValueError("an earlier workspace mutation is incomplete")
        capture_locked(directory, executable)
        if (directory / "remote.json").exists():
            remote_client.ensure_current_revision(directory)
        socket = directory / "kitty.sock"
        before = pane_map(json.loads(query(socket, "ls")))
        if window_id not in before or before[window_id][0] != expected_pane:
            raise ValueError("creation target changed; nothing created")
        if origin and host != origin:
            raise ValueError("remote-origin panes must use the origin host")
        pane = "kzw-" + uuid.uuid4().hex
        pending = directory / "pending-create.json"
        record = {"pane": pane, "host": host, "target": expected_pane, "kind": kind,
                  "request_id": uuid.uuid4().hex, "before": sorted(p for p, _ in before.values()),
                  "workspace": name}
        atomic_write(pending, json.dumps(record) + "\n")
        if (directory / "remote.json").exists():
            remote_client.reserve_pane(directory, pane, record["request_id"])
        elif pane in zmx_command(host, "list", "--short").splitlines():
            raise ValueError("pane ID already belongs to a live zmx session")
        options = ["--type=tab"] if kind == "tab" else ["--next-to", f"id:{window_id}",
                                                         "--location=" + {"right": "hsplit", "below": "vsplit", "split": "split"}[kind]]
        command = [executable, "--pane", pane, "--host", host, "--stage-new"]
        if cwd:
            command += ["--initial-cwd", cwd]
        result = query(socket, "launch", "--match", f"id:{window_id}", *options, *command)
        try:
            new_id = int(result.strip())
        except ValueError:
            raise ValueError("Kitty did not return a window ID; creation record retained") from None
        if new_id in before:
            raise ValueError("Kitty returned an existing window ID; creation record retained")
        expected = dict(before)
        expected[new_id] = (pane, host)
        for _ in range(100):
            try:
                current = pane_map(json.loads(query(socket, "ls")))
                if current == expected:
                    capture_locked(directory, executable, expected_panes=expected)
                    pending.unlink()
                    log_pane_event("tab_created" if kind == "tab" else "pane_created", [pane])
                    return
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            time.sleep(0.1)
        raise ValueError(f"creation could not be verified; inspect {pending} and session {pane}")


@contextmanager
def recovery_lease(directory):
    if not (directory / "remote.json").exists():
        yield
        return
    with open(directory / "lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            detached = True
        except BlockingIOError:
            detached = False
        if not detached:
            remote_client.renew_lease(directory)
            yield
            return
        config = json.loads((directory / "remote.json").read_text())
        remote_client.acquire_lease(directory, config["workspace"], config["host"])
        stop_heartbeat = lambda: None
        try:
            stop_heartbeat = remote_client.start_lease_heartbeat(directory)
            yield
        finally:
            stop_heartbeat()
            try:
                remote_client.release_lease(directory)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f"kzw: remote lease release deferred: {exc}", file=sys.stderr, flush=True)


def resume_partial_close_locked(directory, record, pending):
    """Resume only the exact pane selection journaled before a managed close."""
    before_data = record.get("before")
    entries = record.get("panes")
    kind, target_id = record.get("kind"), record.get("target_id")
    if not isinstance(before_data, dict) or not isinstance(entries, list) or not entries or kind not in ("pane", "tab") or type(target_id) is not int:
        raise ValueError("close journal lacks recovery identities; inspect it manually")
    before = {}
    for key, value in before_data.items():
        if (not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key or
                not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str) or
                not PANE.fullmatch(value[0]) or not isinstance(value[1], str) or
                (value[1] and (value[1].startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", value[1])))):
            raise ValueError("invalid recovery identities in close journal")
        before[int(key)] = tuple(value)
    if len({value[0] for value in before.values()}) != len(before):
        raise ValueError("duplicate pane identities in close journal")
    if any(not isinstance(entry, dict) or set(entry) != {"pane", "host"} or
           not isinstance(entry["pane"], str) or not isinstance(entry["host"], str) for entry in entries):
        raise ValueError("invalid close selection in journal")
    selected_values = {(entry["pane"], entry["host"]) for entry in entries}
    if len(selected_values) != len(entries) or not selected_values.issubset(set(before.values())):
        raise ValueError("invalid close selection in journal")
    selected = {wid for wid, value in before.items() if value in selected_values}
    expected = {wid: value for wid, value in before.items() if wid not in selected}
    if not expected or (kind == "pane" and selected != {target_id}):
        raise ValueError("invalid partial close journal")
    remote = record.get("remote") is True
    if remote != (directory / "remote.json").exists():
        raise ValueError("close journal origin does not match the workspace")
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    socket = directory / "kitty.sock"
    tree = json.loads(query(socket, "ls"))
    current = pane_map(tree)
    if current == before:
        if kind == "tab":
            tabs = [tab for os_window in tree for tab in os_window.get("tabs", []) if tab.get("id") == target_id]
            if len(tabs) != 1 or {window["id"] for window in tabs[0].get("windows", [])} != selected:
                raise ValueError("tab changed since confirmation; close journal retained")
        query(socket, "close-window" if kind == "pane" else "close-tab", "--match", f"id:{target_id}")
        for _ in range(50):
            current = pane_map(json.loads(query(socket, "ls")))
            if current == expected:
                break
            time.sleep(0.1)
    if current != expected:
        raise ValueError("Kitty layout differs from the close journal; no cleanup performed")
    capture_locked(directory, executable, allow_removals=True, expected_panes=expected,
                   confirmed_close=remote, close_panes=[entry["pane"] for entry in entries] if remote else None)
    record["phase"] = "layout-saved"
    atomic_write(pending, json.dumps(record) + "\n")


def retry_close(name, origin=""):
    directory = state_dir(name, origin) if origin else state_dir(name)
    with recovery_lease(directory), capture_lock(directory):
        if not (directory / "pending-close.json").exists():
            raise ValueError("no pending close")
        pending = directory / "pending-close.json"
        record = json.loads(pending.read_text())
        if record.get("phase") == "closing" and record.get("last") is True:
            # The final close was explicitly authorized and journaled before
            # publishing. Replaying the same remote request handles a lost ack.
            if record.get("remote"):
                remote_client.publish(directory, EMPTY_SNAPSHOT, [], confirmed_close=True,
                                      close_panes=[entry["pane"] for entry in record["panes"]])
            atomic_write(directory / "workspace.kitty-session", EMPTY_SNAPSHOT)
            atomic_write(directory / "panes.json", "[]\n")
            record["phase"] = "layout-saved"
            atomic_write(pending, json.dumps(record) + "\n")
        elif record.get("phase") == "closing":
            resume_partial_close_locked(directory, record, pending)
        panes = cleanup_close_locked(directory)
    if record.get("kind") in ("pane", "tab"):
        log_pane_event("tab_destroyed" if record["kind"] == "tab" else "pane_destroyed", panes)
    else:
        for pane in panes:
            log_pane_event("pane_destroyed", [pane])


def retry_create(name, origin=""):
    """Finish a journaled creation only while its complete Kitty view exists."""
    directory = state_dir(name, origin) if origin else state_dir(name)
    executable = shutil.which("kzw-pane")
    if not executable:
        raise ValueError("kzw-pane not found on PATH")
    with recovery_lease(directory), capture_lock(directory):
        pending = directory / "pending-create.json"
        if not pending.is_file() or (directory / "pending-close.json").exists():
            raise ValueError("no recoverable pane creation, or a close is pending")
        record = json.loads(pending.read_text())
        pane, host = record.get("pane"), record.get("host")
        if record.get("kind") == "bootstrap" and record.get("phase") != "launching":
            raise ValueError("bootstrap has not reached the launch phase; state retained")
        if (not isinstance(pane, str) or not PANE.fullmatch(pane) or not isinstance(host, str) or
                (host and (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host)))):
            raise ValueError("invalid pending pane creation")
        current = pane_map(json.loads(query(directory / "kitty.sock", "ls")))
        metadata = directory / "panes.json"
        if not metadata.is_file() and record.get("kind") != "bootstrap":
            raise ValueError("no complete saved pane manifest; inspect the pending creation manually")
        old = set(json.loads(metadata.read_text())) if metadata.is_file() else set()
        if {value[0] for value in current.values()} != old | {pane} or (pane, host) not in current.values():
            raise ValueError("Kitty layout does not match the pending creation; state retained")
        if pane not in zmx_command(host, "list", "--short").splitlines():
            raise ValueError("pending zmx session is missing; state retained for inspection")
        if (directory / "remote.json").exists():
            config = json.loads((directory / "remote.json").read_text())
            remote = remote_client.call(config["host"], {"method": "workspace.get", "workspace": config["workspace"]})
            if pane not in remote.get("panes", []) and pane not in remote.get("reservations", []):
                operation = record.get("request_id")
                if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
                    raise ValueError("pending creation has no reservation ID; inspect it manually")
                remote_client.reserve_pane(directory, pane, operation)
        capture_locked(directory, executable, expected_panes=current)
        pending.unlink()
        if record.get("kind") == "bootstrap":
            remove_bootstrap_session(directory, name, host, executable, pane)
    log_pane_event("tab_created" if record.get("kind") == "tab" else "pane_created", [pane])


def remove_bootstrap_session(directory, name, host, executable, pane):
    """Remove only an unmodified startup file created for this bootstrap."""
    session = directory / "bootstrap.kitty-session"
    if session.is_file() and not session.is_symlink():
        base = "new_tab " + check_name(name) + "\nlayout splits\nlaunch " + shlex.join([executable, "--pane", pane, "--host", host])
        if session.read_text() in (base + "\n", base + " --existing\n"):
            session.unlink()


def prepare_bootstrap(directory, name, host, executable, allow_empty=False):
    """Journal and reserve a remote workspace's first pane before launching it."""
    with capture_lock(directory):
        pending = directory / "pending-create.json"
        session = directory / "bootstrap.kitty-session"
        snapshot = directory / "workspace.kitty-session"
        empty = snapshot.is_file() and snapshot.read_text() == EMPTY_SNAPSHOT
        if (pending.exists() or session.exists() or session.is_symlink() or
                (snapshot.exists() and not (allow_empty and empty)) or (allow_empty and not empty)):
            raise ValueError("workspace initialization is already pending")
        pane = "kzw-" + uuid.uuid4().hex
        record = {"kind": "bootstrap", "phase": "reserving", "pane": pane, "host": host, "workspace": name,
                  "starting_snapshot": "empty" if empty else "dormant", "request_id": uuid.uuid4().hex}
        atomic_write(pending, json.dumps(record) + "\n")
        if (directory / "remote.json").exists():
            remote_client.reserve_pane(directory, pane, record["request_id"])
        elif pane in zmx_command(host, "list", "--short").splitlines():
            raise ValueError("bootstrap pane ID already belongs to a live zmx session")
        record["phase"] = "reserved"
        atomic_write(pending, json.dumps(record) + "\n")
        launch = shlex.join([executable, "--pane", pane, "--host", host])
        atomic_write(session, "new_tab " + check_name(name) + "\nlayout splits\nlaunch " + launch + "\n")
        return session


def recover_bootstrap(directory, name, host, executable):
    """Resume a journaled first pane without recreating an uncertain session."""
    with capture_lock(directory):
        pending = directory / "pending-create.json"
        record = json.loads(pending.read_text())
        pane, phase, operation = record.get("pane"), record.get("phase"), record.get("request_id")
        if (record.get("kind") != "bootstrap" or record.get("host") != host or
                not isinstance(pane, str) or not PANE.fullmatch(pane) or
                not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation) or
                phase not in ("reserving", "reserved", "launching")):
            raise ValueError("bootstrap journal requires manual inspection")
        starting_snapshot = record.get("starting_snapshot", "dormant")
        if starting_snapshot not in ("empty", "dormant"):
            raise ValueError("invalid bootstrap starting state")
        expected_snapshot = EMPTY_SNAPSHOT if starting_snapshot == "empty" else remote_client.DORMANT
        if (directory / "remote.json").exists():
            config = json.loads((directory / "remote.json").read_text())
            if config.get("pending") or config.get("host") != host or config.get("workspace") != name:
                raise ValueError("bootstrap has conflicting or pending remote state")
            remote = remote_client.call(host, {"method": "workspace.get", "workspace": name})
            if (not remote.get("exists") or remote.get("revision") != config.get("revision") or
                    remote.get("snapshot") != expected_snapshot or remote.get("panes") or
                    set(remote.get("reservations", [])) - {pane}):
                raise ValueError("remote workspace changed during bootstrap; state retained")
            if pane not in remote.get("reservations", []):
                if phase != "reserving":
                    raise ValueError("bootstrap reservation is missing; state retained")
                remote_client.reserve_pane(directory, pane, operation)
        else:
            snapshot = directory / "workspace.kitty-session"
            local_state_valid = ((starting_snapshot == "empty" and snapshot.is_file() and snapshot.read_text() == EMPTY_SNAPSHOT) or
                                 (starting_snapshot == "dormant" and not snapshot.exists() and not snapshot.is_symlink()))
            if record.get("workspace") != name or not local_state_valid:
                raise ValueError("local bootstrap has invalid starting state")
        live = pane in zmx_command(host, "list", "--short").splitlines()
        if phase == "launching" and not live:
            raise ValueError("bootstrap session is absent; refusing to recreate it")
        if phase != "launching" and live:
            raise ValueError("unexpected existing bootstrap session; state retained")
        session = directory / "bootstrap.kitty-session"
        initial = "new_tab " + check_name(name) + "\nlayout splits\nlaunch " + shlex.join([executable, "--pane", pane, "--host", host]) + "\n"
        desired = initial if phase != "launching" else initial.rstrip("\n") + " --existing\n"
        if session.is_symlink() or (session.exists() and session.read_text() not in (initial, desired)):
            raise ValueError("bootstrap session file differs from its journal")
        atomic_write(session, desired)
        if phase == "reserving":
            record["phase"] = "reserved"
            atomic_write(pending, json.dumps(record) + "\n")
        return session


def reconcile_bootstrap_commit(directory, name, host, executable):
    """Recover a published first layout whose local acknowledgement was lost."""
    with capture_lock(directory):
        pending = directory / "pending-create.json"
        record = json.loads(pending.read_text())
        pane = record.get("pane")
        if (record.get("kind") != "bootstrap" or record.get("phase") != "launching" or
                record.get("host") != host or not isinstance(pane, str) or not PANE.fullmatch(pane)):
            return False
        config = json.loads((directory / "remote.json").read_text())
        update = config.get("pending")
        if update:
            request = update.get("request", {})
            if (request.get("method") != "workspace.put" or request.get("workspace") != name or
                    request.get("panes") != [pane] or request.get("remove")):
                raise ValueError("pending remote update does not match bootstrap; state retained")
            local_snapshot, parsed = remote_client.translate(request.get("snapshot", ""), executable, host, False)
            if parsed != [pane]:
                raise ValueError("pending bootstrap layout is inconsistent")
            remote_client.publish(directory, local_snapshot, parsed)
        remote = remote_client.call(host, {"method": "workspace.get", "workspace": name})
        if remote.get("snapshot") in (remote_client.DORMANT, EMPTY_SNAPSHOT):
            return False
        if (remote.get("panes") != [pane] or remote.get("reservations") or
                remote_client.translate(remote.get("snapshot", ""), executable, host, False)[1] != [pane]):
            raise ValueError("remote bootstrap layout conflicts with its journal")
        remote_client.pull(directory, name, host, executable)
        pending.unlink()
        # The temporary startup file is retained if modified; the saved
        # authoritative layout is now sufficient for normal reattachment.
        remove_bootstrap_session(directory, name, host, executable, pane)
        return True


def reconcile_local_bootstrap_commit(directory, name, host, executable):
    """Finish local bootstrap bookkeeping only for an intact saved layout."""
    with capture_lock(directory):
        pending = directory / "pending-create.json"
        record = json.loads(pending.read_text())
        pane = record.get("pane")
        if (record.get("kind") != "bootstrap" or record.get("phase") != "launching" or
                record.get("workspace") != name or record.get("host") != host or
                not isinstance(pane, str) or not PANE.fullmatch(pane)):
            return False
        snapshot, metadata = directory / "workspace.kitty-session", directory / "panes.json"
        if not snapshot.is_file() or snapshot.read_text() == EMPTY_SNAPSHOT:
            return False
        if snapshot.is_symlink() or not metadata.is_file() or metadata.is_symlink():
            raise ValueError("local bootstrap snapshot requires manual inspection")
        if json.loads(metadata.read_text()) != [pane]:
            raise ValueError("local bootstrap manifest differs from the journal")
        launches = [shlex.split(line) for line in snapshot.read_text().splitlines() if line.startswith("launch ")]
        if (len(launches) != 1 or len(launches[0]) < 8 or
                not launches[0][1].startswith("kitty-unserialize-data=") or launches[0][-6] != executable or
                launches[0][-5:] != ["--pane", pane, "--host", host, "--existing"]):
            raise ValueError("local bootstrap snapshot differs from the journal")
        pending.unlink()
        remove_bootstrap_session(directory, name, host, executable, pane)
        return True


def mark_bootstrap_launching(directory):
    with capture_lock(directory):
        pending = directory / "pending-create.json"
        record = json.loads(pending.read_text())
        if record.get("kind") != "bootstrap" or record.get("phase") not in ("reserved", "launching"):
            raise ValueError("bootstrap is not ready to launch")
        record["phase"] = "launching"
        atomic_write(pending, json.dumps(record) + "\n")


def finish_bootstrap(directory, executable):
    """Publish the first pane only when Kitty and zmx agree on its identity."""
    with capture_lock(directory):
        pending = directory / "pending-create.json"
        record = json.loads(pending.read_text())
        pane, host = record.get("pane"), record.get("host")
        if (record.get("kind") != "bootstrap" or record.get("phase") != "launching" or
                not isinstance(pane, str) or not PANE.fullmatch(pane) or not isinstance(host, str)):
            raise ValueError("invalid bootstrap creation journal")
        current = pane_map(json.loads(query(directory / "kitty.sock", "ls")))
        if len(current) != 1 or set(current.values()) != {(pane, host)}:
            raise ValueError("waiting for the reserved bootstrap pane")
        if pane not in zmx_command(host, "list", "--short").splitlines():
            raise ValueError("waiting for the reserved bootstrap session")
        capture_locked(directory, executable, expected_panes=current)
        pending.unlink()
        name = record.get("workspace")
        if name is None and (directory / "remote.json").exists():
            name = json.loads((directory / "remote.json").read_text())["workspace"]
        remove_bootstrap_session(directory, name, host, executable, pane)
        log_pane_event("tab_created", [pane])


def kitty_launch_command(args, env):
    """Keep opt-in macOS integration windows hidden and non-activating."""
    if env.get("KZW_TEST_KITTY_BACKGROUND") != "1":
        return args
    if sys.platform != "darwin" or not Path("/Applications/kitty.app").is_dir():
        raise ValueError("background Kitty integration requires /Applications/kitty.app on macOS")
    forwarded = {key: value for key, value in env.items()
                 if key.startswith("KZW_") or key in ("PATH", "PYTHONPATH", "XDG_STATE_HOME", "KITTY_CONFIG_DIRECTORY", "ZMX_DIR", "SHELL")}
    command = ["/usr/bin/open", "-g", "-j", "-n", "-W", "-a", "/Applications/kitty.app"]
    for key in sorted(forwarded):
        command.extend(["--env", key + "=" + forwarded[key]])
    return command + ["--args", *args[1:]]


@remote_client.persistent_rpc()
def run(name, host, remote_attached=False, existing_lock=None, bootstrap_session=None, create_empty=False, appearance=None):
    directory = state_dir(name, host) if remote_attached else state_dir(name)
    if (directory / "remote.json").exists() and not remote_attached:
        raise ValueError("coordinator workspace: use kzw attach NAME (add --host for a remote origin)")
    pane_executable = shutil.which("kzw-pane")
    if not pane_executable:
        raise ValueError("kzw-pane not found; install the project and activate its environment")
    if not shutil.which("kitty"):
        raise ValueError("kitty not found on PATH")
    if not host and not shutil.which("zmx"):
        raise ValueError("zmx not found on PATH; install it before starting a local workspace")
    lock_context = nullcontext(existing_lock) if existing_lock is not None else open(directory / "lock", "a+")
    with lock_context as lock:
        if existing_lock is None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("workspace is already running") from None
        if (directory / "pending-close.json").exists():
            raise ValueError("a pane close is incomplete; inspect it or run kzw retry-close")
        pending_create = directory / "pending-create.json"
        sock = directory / "kitty.sock"
        # The lock proves no kzw supervisor is using this workspace. Refuse a
        # stale socket rather than unlinking anything we cannot verify is ours.
        if sock.exists():
            raise ValueError(f"socket already exists: {sock}; check for a running Kitty before removing it")
        if not remote_attached:
            recover_local_managed_creation(directory, pane_executable)
        recover_local_native_stage(directory, pane_executable)
        check_native_journals(directory)
        if pending_create.exists() and bootstrap_session is None:
            record = json.loads(pending_create.read_text())
            if remote_attached or record.get("kind") != "bootstrap":
                raise ValueError("a pane creation is incomplete; inspect pending-create.json")
            if not reconcile_local_bootstrap_commit(directory, name, host, pane_executable):
                bootstrap_session = recover_bootstrap(directory, name, host, pane_executable)
        if not remote_attached and bootstrap_session is None and not create_empty:
            with capture_lock(directory):
                retire_local_empty(directory)
        snapshot = directory / "workspace.kitty-session"
        empty = snapshot.is_file() and snapshot.read_text() == EMPTY_SNAPSHOT
        if bootstrap_session is not None and (bootstrap_session != directory / "bootstrap.kitty-session" or (snapshot.exists() and not empty)):
            raise ValueError("bootstrap conflicts with an existing workspace snapshot")
        if create_empty and not empty:
            raise ValueError("new-pane requires an empty workspace")
        if empty and not create_empty and bootstrap_session is None:
            print(f"Workspace {name!r} has no live panes")
            return 0
        env = os.environ.copy()
        env["KZW_HOST"] = host
        env["KZW_WORKSPACE"] = name
        env["KZW_WORKSPACE_ORIGIN"] = host if remote_attached else ""
        env["KZW_WORKSPACE_COORDINATOR"] = "1" if remote_attached else ""
        env["KZW_WORKSPACE_DIRECTORY"] = str(directory)
        env["KZW_CHANGE_SIGNAL"] = str(directory / "watcher.changed")
        # Mask Kitty's automatic wrapper title until the process monitor has
        # identified the actual foreground program, including on new tabs.
        args = ["kitty", "--listen-on", f"unix:{sock}", "-o", "allow_remote_control=socket-only",
                "-o", f"shell={pane_executable}", "-o", "enabled_layouts=splits,tall,stack",
                 "-o", "tab_title_template={title.replace('kzw-pane', '…')}  {fmt.nobold}{fmt.noitalic}{index}",
                 "-o", "active_tab_title_template={title.replace('kzw-pane', '…')}  {fmt.nobold}{fmt.noitalic}{index}",
                 "-o", f"watcher={Path(__file__).with_name('watcher.py')}"]
        kitten = Path(__file__).with_name("close_kitten.py")
        create_kitten = Path(__file__).with_name("create_kitten.py")
        detach_kitten = Path(__file__).with_name("detach_kitten.py")
        args += ["-o", f"map super+w kitten {kitten} pane",
                 "-o", f"map kitty_mod+w kitten {kitten} pane",
                 "-o", f"map kitty_mod+q kitten {kitten} tab",
                 "-o", f"map super+shift+w kitten {detach_kitten}",
                 "-o", f"map super+enter kitten {create_kitten} split",
                 "-o", f"map kitty_mod+enter kitten {create_kitten} right",
                 "-o", f"map kitty_mod+alt+enter kitten {create_kitten} below",
                 "-o", "map kitty_mod+up scroll_to_prompt -1",
                 "-o", "map kitty_mod+down scroll_to_prompt 1",
                 "-o", f"map super+t kitten {create_kitten} tab",
                 "-o", f"map kitty_mod+t kitten {create_kitten} tab"]
        for number in range(1, 11):
            args += ["-o", f"map super+{number % 10} goto_tab {number}"]
        if appearance is not None:
            args.extend(appearance["options"])
            if appearance["marker"] is not None:
                env["KZW_INHERIT_MARKER"] = json.dumps(appearance["marker"], separators=(",", ":"))
                print("kzw: inherited marker initializes asynchronously; verify masking in every pane before sharing", file=sys.stderr)
            else:
                env.pop("KZW_INHERIT_MARKER", None)
        else:
            env.pop("KZW_INHERIT_MARKER", None)
        focus = saved_focus(snapshot.read_text()) if snapshot.exists() else None
        if bootstrap_session is not None:
            args += ["--session", str(bootstrap_session)]
            bootstrap = True
        elif create_empty:
            args += ["--session", str(prepare_bootstrap(directory, name, host, pane_executable, allow_empty=True))]
            bootstrap = True
        elif snapshot.exists():
            args += ["--session", str(snapshot)]
            bootstrap = False
        else:
            args += ["--session", str(prepare_bootstrap(directory, name, host, pane_executable))]
            bootstrap = True
        broker = remote_client.ConnectionBroker(host, name) if remote_attached else None
        if broker:
            env["KZW_RPC_SOCKET"] = str(broker.path)
        else:
            env.pop("KZW_RPC_SOCKET", None)
        try:
            title_stop = start_title_monitor(sock, query)
        except Exception:
            if broker:
                broker.close()
            raise
        try:
            status_stop = remote_client.start_status_monitor(directory) if remote_attached else lambda: None
        except Exception:
            title_stop.set()
            if broker:
                broker.close()
            raise
        try:
            if bootstrap:
                mark_bootstrap_launching(directory)
            child = subprocess.Popen(kitty_launch_command(args, env), env=env, stdin=subprocess.DEVNULL)
        except Exception:
            status_stop()
            title_stop.set()
            if broker:
                broker.close()
            raise
        print(f"Workspace {name!r} started. Keep this supervisor running; close Kitty to detach.", flush=True)
        last_error = None
        pending_focus = focus
        change_signal = directory / "watcher.changed"
        last_signal = None
        next_capture = 0.0
        next_exit_check = 0.0
        remote_signal = directory / "remote-status.changed"
        last_remote_signal = None
        remote_hint = []
        remote_revision = None
        next_bootstrap = 0.0
        workspace_observed = False
        empty_since = None
        next_view_check = 0.0
        source_window = supervisor_window.source_window()
        minimize_attempted = False
        try:
            while child.poll() is None:
                try:
                    if bootstrap:
                        if time.monotonic() >= next_bootstrap:
                            next_bootstrap = time.monotonic() + 1
                            finish_bootstrap(directory, pane_executable)
                            bootstrap = False
                        if bootstrap:
                            time.sleep(0.1)
                            continue
                    if workspace_observed and time.monotonic() >= next_view_check:
                        next_view_check = time.monotonic() + 1
                        current = json.loads(query(sock, "ls"))
                        native = directory / "native-pending"
                        unfinished = ((directory / "pending-create.json").exists() or (directory / "pending-close.json").exists() or
                                      (native.exists() and (not native.is_dir() or any(native.iterdir()))))
                        if not current and not unfinished:
                            if empty_since is None:
                                empty_since = time.monotonic()
                            elif time.monotonic() - empty_since >= 2:
                                # The workspace view is gone, but Kitty may still
                                # own unrelated utility windows. Never close them.
                                break
                        else:
                            empty_since = None
                    if pending_focus:
                        restore_focus(sock, pending_focus)
                        pending_focus = None
                    try:
                        signal = change_signal.stat().st_mtime_ns
                    except FileNotFoundError:
                        signal = None
                    try:
                        remote_signal_time = remote_signal.stat().st_mtime_ns if remote_attached else None
                    except FileNotFoundError:
                        remote_signal_time = None
                    remote_changed = remote_signal_time != last_remote_signal
                    if remote_changed:
                        data = json.loads(remote_signal.read_text()) if remote_signal_time is not None else {"missing": []}
                        remote_hint = data["missing"]
                        if not isinstance(remote_hint, list) or any(not isinstance(p, str) or not PANE.fullmatch(p) for p in remote_hint):
                            raise ValueError("invalid remote status notification")
                        remote_revision = data.get("revision")
                        if remote_revision is not None and (type(remote_revision) is not int or remote_revision < 0):
                            raise ValueError("invalid remote status revision")
                        last_remote_signal = remote_signal_time
                    if remote_revision is not None:
                        config = json.loads((directory / "remote.json").read_text())
                        if remote_revision > config.get("revision", -1) and not config.get("pending"):
                            raise ValueError("remote workspace revision changed; reconnect before changing the view")
                    if remote_changed or signal != last_signal or time.monotonic() >= next_exit_check:
                        synchronize_exits(directory, pane_executable, remote_hint)
                        next_exit_check = time.monotonic() + 1
                    if signal != last_signal or time.monotonic() >= next_capture:
                        capture(directory, pane_executable)
                        workspace_observed = True
                        last_signal = signal
                        next_capture = time.monotonic() + 1
                        if source_window and not minimize_attempted:
                            # A lost response may still mean minimization happened;
                            # always attempt restoration when the workspace ends.
                            minimize_attempted = True
                            try:
                                supervisor_window.change(source_window, "minimize")
                            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                                print(f"kzw: could not minimize supervisor window: {exc}", file=sys.stderr, flush=True)
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
            try:
                status_stop()
                title_stop.set()
                if broker:
                    broker.close()
            finally:
                if minimize_attempted:
                    try:
                        supervisor_window.change(source_window, "restore")
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        print(f"kzw: restore the supervisor window from the Dock: {exc}", file=sys.stderr, flush=True)
    return child.returncode if child.returncode is not None else 0


def create_remote(name, host):
    directory = state_dir(name, host)
    with open(directory / "lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace is already running") from None
        remote_client.create(directory, name, host)
    location = f" on {host}" if host else " locally"
    print(f"Created dormant coordinator workspace {name!r}{location}; use kzw attach to open it")


def rename_workspace(name, destination, host=""):
    """Rename detached coordinator state; retain existing client caches."""
    check_name(name)
    check_name(destination)
    if name == destination:
        raise ValueError("destination must differ from the current workspace name")
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kzw"
    from .sessions import coordinator_request

    if host:
        call = lambda request: remote_client.call(host, request)
        route = "ssh:" + host + ":" + remote_client.origin_state_home(host)
        cache = True
    else:
        # Host-local remote-origin coordinators and local coordinators occupy
        # distinct sockets; never silently select between two matching names.
        pending_key = hashlib.sha256(json.dumps(["host-origin", name, destination], separators=(",", ":")).encode()).hexdigest()
        origin_pending = (root / "rename-intents" / (pending_key + ".json")).exists()
        origin = None
        if (root / "server.sock").exists():
            origin = coordinator_request({"method": "workspace.get", "workspace": name})
        local = None
        if (root / "coordinator" / "remote.sqlite3").is_file():
            local = remote_client.call("", {"method": "workspace.get", "workspace": name})
        if origin and origin.get("exists") and local and local.get("exists"):
            raise ValueError("rename is ambiguous: workspace exists in both host-origin and local coordinators")
        if origin_pending or (origin is not None and (origin.get("exists") or not local or not local.get("exists"))):
            if origin is None:
                raise ValueError("host-origin rename is pending but its coordinator is unavailable")
            call, route, cache = coordinator_request, "host-origin", False
        else:
            call = lambda request: remote_client.call("", request)
            route, cache = "local-coordinator", True

    intents = root / "rename-intents"
    intents.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = intents.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe workspace rename journal directory")
    key = hashlib.sha256(json.dumps([route, name, destination], separators=(",", ":")).encode()).hexdigest()
    journal = intents / (key + ".json")
    # A stable lock also serializes retry of a request whose reply was lost.
    with ExitStack() as stack:
        journal_lock = stack.enter_context(open(intents / (key + ".lock"), "a+"))
        try:
            fcntl.flock(journal_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace rename is already in progress") from None
        if cache:
            paths = [state_dir(name, host), state_dir(destination, host)]
            for directory in sorted(paths):
                lock = stack.enter_context(open(directory / "lock", "a+"))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("workspace is attached locally; detach before renaming") from None
            source, target = paths
            if any((source / item).exists() for item in ("kitty.sock", "pending-create.json", "pending-close.json", "native-stage.json")):
                raise ValueError("workspace has an active view or unfinished local operations")
            native = source / "native-pending"
            if native.exists() and (native.is_symlink() or not native.is_dir() or any(native.iterdir())):
                raise ValueError("workspace has unfinished local pane creation")
            config = source / "remote.json"
            if config.exists():
                value = json.loads(config.read_text())
                if (value.get("workspace") != name or value.get("host") != host or value.get("pending") or value.get("creating") or
                        value.get("state_home", "") != remote_client.origin_state_home(host)):
                    raise ValueError("workspace has conflicting or pending local coordinator state")
            elif (source / "workspace.kitty-session").exists():
                raise ValueError("legacy workspace cannot be renamed by the coordinator")
            if not remote_client.inert_legacy_artifacts(target):
                # A successful remote rename may have written this cache just
                # before a crash. Only its own durable journal can authorize a
                # retry; never adopt an unrelated destination cache.
                target_config = target / "remote.json"
                if not journal.is_file() or target_config.is_symlink() or not target_config.is_file():
                    raise ValueError("destination has existing local workspace state")
                pending_record = json.loads(journal.read_text())
                cached = json.loads(target_config.read_text())
                if (cached.get("rename_request") != pending_record.get("request_id") or
                        cached.get("workspace") != destination or cached.get("host") != host or
                        cached.get("pending") or cached.get("creating") or
                        not remote_client.inert_legacy_artifacts(target, ignored_names=("remote.json",))):
                    raise ValueError("destination has existing local workspace state")
        if journal.exists():
            record = json.loads(journal.read_text())
            if (record.get("route"), record.get("workspace"), record.get("destination")) != (route, name, destination):
                raise ValueError("workspace rename journal conflicts with this request")
        else:
            state = call({"method": "workspace.get", "workspace": name})
            if not state.get("exists"):
                raise ValueError("workspace does not exist")
            record = {"route": route, "workspace": name, "destination": destination,
                      "revision": state["revision"], "request_id": uuid.uuid4().hex}
            atomic_write(journal, json.dumps(record, separators=(",", ":")) + "\n")
        request = {"method": "workspace.rename", "workspace": name, "destination": destination,
                   "revision": record["revision"], "request_id": record["request_id"]}
        try:
            result = call(request)
        except remote_client.RemoteRejected:
            # An explicit rejection is a rolled-back transaction, unlike an
            # interrupted connection whose outcome must be replayed.
            journal.unlink()
            raise
        if cache:
            if type(result.get("revision")) is not int or result.get("name") != destination:
                raise ValueError("invalid workspace rename response; retry the pending rename")
            remote_client.save_config(target, {"workspace": destination, "host": host,
                                              "revision": result["revision"],
                                              "state_home": remote_client.origin_state_home(host),
                                              "rename_request": record["request_id"]})
        journal.unlink()
    print(f"Renamed workspace {name!r} to {destination!r}; zmx sessions are unchanged")


def kill_workspace(name, host):
    """Confirm and durably schedule all coordinator-owned sessions for cleanup."""
    directory = state_dir(name, host)
    with open(directory / "lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace is attached locally; detach it before killing it") from None
        if not host and (directory / "workspace.kitty-session").exists() and not (directory / "remote.json").exists():
            raise ValueError("kill currently supports coordinator workspaces only; this is a legacy workspace")
        if any((directory / item).exists() for item in
               ("kitty.sock", "pending-close.json", "pending-create.json", "native-stage.json")):
            raise ValueError("workspace may be active or has unfinished local operations")
        pending = directory / "native-pending"
        if pending.exists() and (not pending.is_dir() or any(pending.iterdir())):
            raise ValueError("workspace has unfinished local pane creation")
        state = remote_client.call(host, {"method": "workspace.get", "workspace": name})
        if not state.get("exists"):
            raise ValueError("workspace does not exist")
        panes = state["panes"]
        if input(f"Kill workspace {name!r} and terminate its {len(panes)} zmx session(s) and all their programs? [y/N] ").strip().lower() != "y":
            print("Cancelled")
            return
        remote_client.acquire_lease(directory, name, host)
        try:
            token = remote_client.lease_token(directory)
            remote_client.call(host, {"method": "workspace.kill", "workspace": name,
                                      "revision": state["revision"], "lease_token": token})
            result = remote_client.call(host, {"method": "cleanup.retry", "workspace": name, "lease_token": token})
            if result["remaining"]:
                raise ValueError("workspace kill is pending cleanup; the server will retry")
        finally:
            remote_client.release_lease(directory)
    print(f"Workspace {name!r} termination scheduled; the coordinator will retire it after verified cleanup")


def attach_remote(name, host, create_empty=False, appearance=None):
    directory = state_dir(name, host)
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
        pending = directory / "pending-create.json"
        recovering_bootstrap = False
        recovering_managed = False
        if pending.exists():
            record = json.loads(pending.read_text())
            recovering_bootstrap = record.get("kind") == "bootstrap"
            recovering_managed = not recovering_bootstrap
        remote_client.acquire_lease(directory, name, host)
        stop_heartbeat = lambda: None
        appearance_arg = {"appearance": appearance} if appearance is not None else {}
        try:
            stop_heartbeat = remote_client.start_lease_heartbeat(directory)
            if recovering_managed:
                if create_empty:
                    raise ValueError("a pane creation is already pending")
                recover_remote_managed_creation(directory, name, host, executable)
                return run(name, host, remote_attached=True, existing_lock=lock, **appearance_arg)
            if recovering_bootstrap:
                if create_empty:
                    raise ValueError("a bootstrap is already pending; attach to recover it")
                if reconcile_bootstrap_commit(directory, name, host, executable):
                    return run(name, host, remote_attached=True, existing_lock=lock, **appearance_arg)
                session = recover_bootstrap(directory, name, host, executable)
                return run(name, host, remote_attached=True, existing_lock=lock, bootstrap_session=session, **appearance_arg)
            remote_client.pull(directory, name, host, executable)
            return run(name, host, remote_attached=True, existing_lock=lock, create_empty=True, **appearance_arg) if create_empty else run(name, host, remote_attached=True, existing_lock=lock, **appearance_arg)
        finally:
            stop_heartbeat()
            try:
                remote_client.release_lease(directory)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f"kzw: remote lease release deferred: {exc}", file=sys.stderr, flush=True)


def remote_snapshot(name, host):
    """Read the authoritative remote layout and journal cursor atomically."""
    check_name(name)
    result = remote_client.call(host, {"method": "workspace.sync", "workspace": name})
    if not result.get("exists"):
        raise ValueError("remote workspace does not exist")
    if (type(result.get("revision")) is not int or result["revision"] < 0 or
            type(result.get("event_cursor")) is not int or result["event_cursor"] < 0 or
            not isinstance(result.get("snapshot"), str) or not isinstance(result.get("panes"), list) or
            any(not isinstance(p, str) or not PANE.fullmatch(p) for p in result["panes"]) or
            len(set(result["panes"])) != len(result["panes"])):
        raise ValueError("invalid remote workspace snapshot")
    focus = saved_focus(result["snapshot"])
    if focus is not None and focus not in result["panes"]:
        raise ValueError("remote snapshot focus is not in its pane manifest")
    return {**result, "focused_pane": focus}


def inspect_remote(name, host, as_json=False):
    result = remote_snapshot(name, host)
    view = {"workspace": name, "origin": host, **result}
    if as_json:
        print(json.dumps(view, separators=(",", ":")), flush=True)
    else:
        print(f"workspace: {name}\norigin: {host}\nrevision: {result['revision']}\nevent_cursor: {result['event_cursor']}")
        print("panes: " + (", ".join(result["panes"]) or "(none)"))
        print("focused_pane: " + (result["focused_pane"] or "(none)"))
        print("layout (portable Kitty session):\n" + result["snapshot"], end="")


def events_remote(name, host, after=None, once=False, as_json=False):
    """Stream journaled observations; the initial snapshot establishes a cursor."""
    if after is not None and (type(after) is not int or after < 0):
        raise ValueError("event cursor must be nonnegative")
    with remote_client.persistent_rpc():
        snapshot = remote_snapshot(name, host)
        if after is None:
            cursor = snapshot["event_cursor"]
            message = {"type": "snapshot", "workspace": name, "origin": host, **snapshot}
            if as_json:
                print(json.dumps(message, separators=(",", ":")), flush=True)
            else:
                print(f"snapshot:{name} revision={snapshot['revision']} cursor={cursor}", flush=True)
            if once:
                return
        else:
            cursor = after
            if cursor > snapshot["event_cursor"]:
                raise ValueError("event cursor is ahead of this workspace's journal")
        while True:
            request = {"method": "workspace.events.wait", "workspace": name, "after": cursor,
                       "limit": 100, "timeout": 0 if once else 15}
            try:
                result = remote_client.call(host, request)
            except (OSError, subprocess.SubprocessError) as exc:
                if once:
                    raise
                print(f"kzw: event stream interrupted; retrying from {cursor}: {exc}", file=sys.stderr, flush=True)
                time.sleep(1)
                continue
            events = result.get("events")
            if not isinstance(events, list):
                raise ValueError("invalid remote event page")
            for event in events:
                if (not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] <= cursor or
                        type(event.get("revision")) is not int or not isinstance(event.get("kind"), str) or
                        not isinstance(event.get("data"), dict)):
                    raise ValueError("invalid or out-of-order remote event")
                message = {"type": event["kind"], "workspace": name, "origin": host,
                           "seq": event["seq"], "revision": event["revision"], "data": event["data"]}
                if as_json:
                    print(json.dumps(message, separators=(",", ":")), flush=True)
                else:
                    print(f"{event['seq']} revision={event['revision']} {event['kind']} " +
                          json.dumps(event["data"], separators=(",", ":")), flush=True)
                cursor = event["seq"]
                if event["kind"] == "workspace.retired" or (event["kind"] == "workspace.renamed" and
                                                             event["data"].get("from") == name):
                    return
            if once and len(events) < 100:
                return


def main():
    parser = argparse.ArgumentParser(prog="kzw")
    commands = parser.add_subparsers(dest="command", required=True)
    sessions_parser = commands.add_parser("sessions", help="select a session from a workspace on this host")
    sessions_commands = sessions_parser.add_subparsers(dest="sessions_command", required=True)
    sessions_list = sessions_commands.add_parser("list", help="select a local zmx session to attach")
    sessions_list.add_argument("name")
    sessions_commands.add_parser("bash-hook", help="print the optional Bash command-tracking hook")
    sessions_record = sessions_commands.add_parser("_record", help=argparse.SUPPRESS)
    sessions_record.add_argument("pane")
    sessions_record.add_argument("pid", type=int)
    start = commands.add_parser("start", help="open a workspace and supervise snapshots")
    start.add_argument("name")
    start.add_argument("--host", default="", help="SSH host for newly created panes")
    start.add_argument("--inherit-appearance", action="store_true", help="copy colors, font size and active function marker from Kitty")
    create_parser = commands.add_parser("create", help="create a dormant remote workspace")
    create_parser.add_argument("name")
    create_parser.add_argument("--host", default="", help="remote workspace origin; omit for local coordinator")
    create_parser.add_argument("--attach", action="store_true", help="attach immediately after creating the workspace")
    create_parser.add_argument("--inherit-appearance", action="store_true", help="copy Kitty appearance when used with --attach")
    attach_parser = commands.add_parser("attach", help="attach a remote workspace")
    attach_parser.add_argument("name")
    attach_parser.add_argument("--host", default="", help="remote workspace origin; omit for local coordinator")
    attach_parser.add_argument("--inherit-appearance", action="store_true", help="copy colors, font size and active function marker from Kitty")
    inspect_parser = commands.add_parser("inspect", help="inspect the authoritative coordinator layout")
    inspect_parser.add_argument("name")
    inspect_parser.add_argument("--host", default="", help="remote workspace origin; omit for local coordinator")
    inspect_parser.add_argument("--json", action="store_true")
    events_parser = commands.add_parser("events", help="stream coordinator workspace observations")
    events_parser.add_argument("name")
    events_parser.add_argument("--host", default="", help="remote workspace origin; omit for local coordinator")
    events_parser.add_argument("--json", action="store_true")
    events_parser.add_argument("--after", type=int, help="replay events after this sequence number")
    events_parser.add_argument("--once", action="store_true", help="print available state or events and exit")
    new_pane_parser = commands.add_parser("new-pane", help="explicitly start a shell in an empty workspace")
    new_pane_parser.add_argument("name")
    new_pane_parser.add_argument("--host", default="", help="remote workspace origin")
    new_pane_parser.add_argument("--shell-host", default="", help="SSH host for a local-state workspace's new shell")
    new_pane_parser.add_argument("--inherit-appearance", action="store_true", help="copy colors, font size and active function marker from Kitty")
    save = commands.add_parser("save", help="save a live workspace explicitly")
    save.add_argument("name")
    save.add_argument("--allow-removals", action="store_true")
    save.add_argument("--host", default="", help="remote workspace origin")
    det = commands.add_parser("detach", help="save the current focus and close the workspace view")
    det.add_argument("name")
    det.add_argument("--host", default="", help="remote workspace origin")
    close = commands.add_parser("close-pane", help="explicitly close a Kitty pane and its zmx session")
    close.add_argument("name")
    close.add_argument("--window-id", type=int, required=True)
    close.add_argument("--host", default="", help="remote workspace origin")
    close_tab_parser = commands.add_parser("close-tab", help="explicitly close a Kitty tab and all of its zmx sessions")
    close_tab_parser.add_argument("name")
    close_tab_parser.add_argument("--tab-id", type=int, required=True)
    close_tab_parser.add_argument("--host", default="", help="remote workspace origin")
    retry = commands.add_parser("retry-close", help="retry cleanup after a saved pane close")
    retry.add_argument("name")
    retry.add_argument("--host", default="", help="remote workspace origin")
    retry_create_parser = commands.add_parser("retry-create", help="finish an interrupted pane creation in a live Kitty view")
    retry_create_parser.add_argument("name")
    retry_create_parser.add_argument("--host", default="", help="remote workspace origin")
    kill_parser = commands.add_parser("kill", help="terminate a detached coordinator workspace and its sessions")
    kill_parser.add_argument("name")
    kill_parser.add_argument("--host", default="", help="remote workspace origin")
    rename_parser = commands.add_parser("rename", help="rename a detached coordinator workspace")
    rename_parser.add_argument("name")
    rename_parser.add_argument("destination")
    rename_parser.add_argument("--host", default="", help="remote workspace origin")
    confirmed = commands.add_parser("_confirmed-close", help=argparse.SUPPRESS)
    confirmed.add_argument("name")
    confirmed.add_argument("kind", choices=("pane", "tab"))
    confirmed.add_argument("target_id", type=int)
    confirmed.add_argument("selection")
    confirmed.add_argument("--host", default="")
    create_managed = commands.add_parser("_managed-create", help=argparse.SUPPRESS)
    create_managed.add_argument("name")
    create_managed.add_argument("kind", choices=("right", "below", "split", "tab"))
    create_managed.add_argument("window_id", type=int)
    create_managed.add_argument("pane")
    create_managed.add_argument("--host", default="")
    create_managed.add_argument("--origin", default="")
    create_managed.add_argument("--cwd-uri", default="")
    list_parser = commands.add_parser("list", help="list saved workspaces")
    list_parser.add_argument("--host", default="")
    server_stop = commands.add_parser("server-stop", help="stop the remote workspace coordinator")
    server_stop.add_argument("--host", default="", help="remote workspace origin; omit for local coordinator")
    args = parser.parse_args()
    try:
        appearance_arg = {}
        if getattr(args, "inherit_appearance", False):
            if args.command == "create" and not args.attach:
                raise ValueError("--inherit-appearance requires --attach when creating a workspace")
            from .appearance import capture
            appearance_arg["appearance"] = capture()
        if args.command == "sessions":
            if args.sessions_command == "bash-hook":
                sys.stdout.write(Path(__file__).with_name("bash_hook.sh").read_text())
                return 0
            if args.sessions_command == "_record":
                from .session_meta import record
                record(args.pane, args.pid, sys.stdin.read(16384))
                return 0
            from .sessions import list_and_attach
            return list_and_attach(args.name)
        if args.command == "start":
            if not args.host:
                directory = state_dir(args.name)
                config = directory / "remote.json"
                if config.is_file() and json.loads(config.read_text()).get("host") == "":
                    return attach_remote(args.name, "", **appearance_arg)
            return run(args.name, args.host, **appearance_arg)
        if args.command == "create":
            create_remote(args.name, args.host)
            if args.attach:
                return attach_remote(args.name, args.host, **appearance_arg)
            return 0
        if args.command == "kill":
            kill_workspace(args.name, args.host)
            return 0
        if args.command == "rename":
            rename_workspace(args.name, args.destination, args.host)
            return 0
        if args.command == "attach":
            return attach_remote(args.name, args.host, **appearance_arg)
        if args.command == "inspect":
            inspect_remote(args.name, args.host, args.json)
            return 0
        if args.command == "events":
            events_remote(args.name, args.host, args.after, args.once, args.json)
            return 0
        if args.command == "new-pane":
            if args.host and args.shell_host:
                raise ValueError("--shell-host is only for local-state workspaces")
            if not args.host and not args.shell_host:
                config = state_dir(args.name) / "remote.json"
                if config.is_file() and json.loads(config.read_text()).get("host") == "":
                    return attach_remote(args.name, "", create_empty=True, **appearance_arg)
            return attach_remote(args.name, args.host, create_empty=True, **appearance_arg) if args.host else run(args.name, args.shell_host, create_empty=True, **appearance_arg)
        if args.command == "server-stop":
            if input("Stop the workspace server? Connected clients must detach first. [y/N] ").strip().lower() != "y":
                print("Cancelled")
                return 0
            remote_client.call(args.host, {"method": "server.shutdown"})
            print("Workspace server is stopping; zmx sessions are preserved")
            return 0
        if args.command == "save":
            executable = shutil.which("kzw-pane")
            if not executable:
                raise ValueError("kzw-pane not found on PATH")
            directory = state_dir(args.name, args.host) if args.host else state_dir(args.name)
            print(f"Saved {capture(directory, executable, args.allow_removals)} panes")
            return 0
        if args.command == "detach":
            detach(args.name, args.host)
            return 0
        if args.command == "close-pane":
            if input("Close this pane and terminate its shell and programs? [y/N] ").strip().lower() != "y":
                print("Cancelled")
                return 0
            close_pane(args.name, args.window_id, args.host)
            return 0
        if args.command == "close-tab":
            if input("Close this tab and terminate all its shells and programs? [y/N] ").strip().lower() != "y":
                print("Cancelled")
                return 0
            close_tab(args.name, args.tab_id, args.host)
            return 0
        if args.command == "_confirmed-close":
            selection = json.loads(args.selection)
            if not isinstance(selection, dict) or not selection:
                raise ValueError("invalid confirmed selection")
            close_target(args.name, args.kind, args.target_id, args.host, selection)
            return 0
        if args.command == "_managed-create":
            create_pane(args.name, args.kind, args.window_id, args.pane, args.host, args.origin, args.cwd_uri)
            return 0
        if args.command == "retry-close":
            retry_close(args.name, args.host)
            return 0
        if args.command == "retry-create":
            retry_create(args.name, args.host)
            return 0
        if args.host:
            for workspace in remote_client.call(args.host, {"method": "workspace.list"})["workspaces"]:
                print(workspace["name"])
            return 0
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kzw"
        names = set()
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if ((path / "workspace.kitty-session").is_file() and not (path / "remote.json").exists()
                        and not local_empty_retirable(path)):
                    names.add(path.name)
            if (root / "coordinator" / "remote.sqlite3").is_file():
                names.update(item["name"] for item in remote_client.call("", {"method": "workspace.list"})["workspaces"])
        for name in sorted(names):
            print(name)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, EOFError, ValueError, subprocess.SubprocessError) as exc:
        print(f"kzw: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
