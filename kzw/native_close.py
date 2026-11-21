"""Record confirmed Kitty pane closes; reconcile them outside the GUI thread."""

import json
import os
from pathlib import Path
import shlex
import stat


RECEIPTS = "native-close.jsonl"


def on_close(boss, window, data):
    """Kitty watcher entry point, loaded directly from this file."""
    if data.get("reason") != "window" or data.get("cause") != "closed" or data.get("confirmed") is not True:
        return
    variables = window.user_vars
    pane, directory = variables.get("kzw_pane"), variables.get("kzw_directory")
    if not pane or not directory or not os.path.isabs(directory):
        return
    directory = Path(directory)
    if variables.get("kzw_workspace") != directory.name:
        return
    try:
        path = directory / "view.json"
        if path.is_symlink():
            return
        view = json.loads(path.read_text())
        if (view.get("instance_pid"), view.get("os_window_id")) != (os.getpid(), window.os_window_id):
            return
        if not view.get("generation"):
            return
        event = [view["generation"], window.id, pane, variables.get("kzw_host", "")]
        data = (json.dumps(event, separators=(",", ":")) + "\n").encode()
        fd = os.open(directory / RECEIPTS, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                     os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.write(fd, data)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        # A lost notification must preserve sessions, not block or undo Kitty's close.
        return


def snapshot_mapping(snapshot):
    from .core import FLAG, PANE, pane_map

    windows = []
    for line in snapshot.splitlines():
        if not line.startswith("launch "):
            continue
        args = shlex.split(line)
        if (len(args) < 7 or not args[1].startswith(FLAG) or args[-5] != "--pane" or
                args[-3] != "--host" or args[-1] != "--existing" or not PANE.fullmatch(args[-4])):
            raise ValueError("native close requires a complete captured layout")
        windows.append({"id": json.loads(args[1][len(FLAG):])["id"],
                        "user_vars": {"kzw_pane": args[-4], "kzw_host": args[-2]}})
    if not windows:
        return {}
    result = pane_map([{"tabs": [{"windows": windows}]}])
    if len(result) != len(windows):
        raise ValueError("duplicate native close snapshot identities")
    return result


def finish_locked(directory, record, pending):
    """Publish a verified plan and leave the standard cleanup journal on error."""
    from . import cli

    snapshot = record["snapshot"]
    after = snapshot_mapping(snapshot)
    if record["last"] != (not after) or (not after and snapshot != cli.EMPTY_SNAPSHOT):
        raise ValueError("invalid empty native close layout")
    before = {int(wid): tuple(identity) for wid, identity in record["before"].items()}
    removed = {(entry["pane"], entry["host"]) for entry in record["panes"]}
    if not removed or not removed.issubset(before.values()) or after != {
            wid: identity for wid, identity in before.items() if identity not in removed}:
        raise ValueError("native close plan does not match its exact removal")
    if record["remote"] != (directory / "remote.json").is_file():
        raise ValueError("native close origin changed")
    if record["remote"]:
        cli.remote_client.publish(directory, snapshot, [pane for pane, _ in after.values()],
                                  confirmed_close=True, close_panes=[pane for pane, _ in removed])
    cli.atomic_write(directory / "workspace.kitty-session", snapshot)
    cli.atomic_write(directory / "panes.json", json.dumps(sorted(pane for pane, _ in after.values())) + "\n")
    record["phase"] = "layout-saved"
    cli.atomic_write(pending, json.dumps(record) + "\n")


def synchronize(directory, executable):
    from . import cli
    from .exits import snapshot_for_exit

    path = directory / RECEIPTS
    if not path.is_file():
        return
    with cli.capture_lock(directory):
        pending = directory / "pending-close.json"
        if path.is_symlink() or pending.is_symlink():
            raise ValueError("unsafe native close state")
        if pending.exists():
            return  # Once journaled, uncertainty is handled by retry-close.
        if any((directory / item).exists() for item in
               ("pending-create.json", "pending-exit.json", "pending-local-kill.json", "native-stage.json")):
            return
        native = directory / "native-pending"
        if native.exists() and (not native.is_dir() or any(native.iterdir())):
            return
        view = cli.view_state(directory)
        if not view or not view.get("generation"):
            return
        saved = directory / "workspace.kitty-session"
        if not saved.is_file():
            return
        before = snapshot_mapping(saved.read_text())
        if not before:
            return
        if set(json.loads((directory / "panes.json").read_text())) != {pane for pane, _ in before.values()}:
            raise ValueError("native close snapshot and pane manifest differ")
        receipts = {}
        # One append per event; tolerate a concurrent append or interrupted write
        # by ignoring incomplete lines. Do not use stale receipts after reattach.
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    continue
                try:
                    generation, wid, pane, host = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if (generation == view["generation"] and type(wid) is int and
                        before.get(wid) == (pane, host)):
                    receipts[wid] = (pane, host)
        if not receipts:
            return
        raw = cli.list_os_windows(view["address"])  # Socket failure is not an empty view.
        tree = cli.workspace_tree([w for w in raw if w["id"] == view["os_window_id"]])
        current = cli.pane_map(tree) if tree and any(t["windows"] for t in tree[0]["tabs"]) else {}
        if any(before.get(wid) != identity for wid, identity in current.items()):
            return
        removed = {wid: identity for wid, identity in before.items() if wid not in current}
        if not removed or any(receipts.get(wid) != identity for wid, identity in removed.items()):
            return
        # A detached/moved pane cannot be treated as a closed session.
        visible = {w.get("user_vars", {}).get("kzw_pane") for ow in raw for t in ow["tabs"] for w in t["windows"]}
        if any(pane in visible for pane, _ in removed.values()):
            return
        remote = (directory / "remote.json").is_file()
        if remote:
            config = json.loads((directory / "remote.json").read_text())
            if (config.get("pending") or set(config.get("panes", [])) != {pane for pane, _ in before.values()} or
                    any(host != config["host"] for _, host in before.values())):
                return
            cli.remote_client.ensure_current_revision(directory)
        if tree:
            tree[0]["last_focused"] = True
        snapshot = snapshot_for_exit(directory, executable, tree) if current else cli.EMPTY_SNAPSHOT
        record = {"phase": "closing", "kind": "native", "last": not current, "remote": remote,
                  "snapshot": snapshot, "before": before,
                  "panes": [{"pane": pane, "host": host} for pane, host in removed.values()]}
        cli.atomic_write(pending, json.dumps(record) + "\n")
        finish_locked(directory, record, pending)
        closed = cli.cleanup_close_locked(directory)
    cli.log_pane_event("pane_destroyed", closed)
