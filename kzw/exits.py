"""Acknowledge origin-proven exits before closing their exact Kitty views."""

import json
import os
from pathlib import Path
import subprocess
from contextlib import nullcontext

from . import remote_client
from .core import atomic_write, pane_map
from .layout_prune import prune_snapshot


def windows(tree):
    return {str(w["id"]): {"id": w["id"], "pid": w["pid"], "os_window_id": ow["id"],
                           "pane": w.get("user_vars", {}).get("kzw_pane"),
                           "ended": w.get("user_vars", {}).get("kzw_attach_ended")}
            for ow in tree for tab in ow["tabs"] for w in tab["windows"]}


def wrappers_gone(targets):
    for target in targets.values():
        try:
            os.kill(target["pid"], 0)
        except ProcessLookupError:
            continue
        return False
    return True


def current_windows(directory, targets):
    from . import cli
    try:
        return windows(json.loads(cli.view_query(directory, "ls")))
    except (OSError, ValueError, subprocess.SubprocessError):
        # Used only AFTER the server commits the exit removal. A private Kitty
        # may disappear with its last pane before replying to the close request.
        if wrappers_gone(targets):
            return {}
        raise


def synchronize(directory, executable, locked=False):
    from . import cli
    if not (directory / "remote.json").is_file():
        return 0  # Legacy mixed-origin layouts lack this coordinator contract.
    with nullcontext() if locked else cli.capture_lock(directory):
        journal = directory / "pending-exit.json"
        if journal.is_symlink():
            raise ValueError("unsafe exit journal")
        if not journal.exists():
            if any((directory / item).exists() for item in
                   ("pending-create.json", "pending-close.json", "native-stage.json", "bootstrap.kitty-session")):
                return 0
            native = directory / "native-pending"
            if native.exists() and (not native.is_dir() or any(native.iterdir())):
                return 0
            config = json.loads((directory / "remote.json").read_text())
            if config.get("pending") or not config.get("panes"):
                return 0
            tree = json.loads(cli.view_query(directory, "ls"))
            mapping = pane_map(tree)
            if {p for p, host in mapping.values()} != set(config["panes"]):
                return 0  # Partial restore, native creation or teardown.
            views = windows(tree)
            candidates = {w["pane"] for w in views.values() if w["ended"] == "1"}
            if not candidates:
                return 0
            receipts = remote_client.exit_receipts(directory, candidates)
            if not receipts:
                return 0
            # Capture every split before pruning the selected leaves. This may
            # advance the revision; publication validates the receipts again.
            cli.capture_locked(directory, executable, expected_panes=mapping)
            source = (directory / "workspace.kitty-session").read_text()
            if windows(json.loads(cli.view_query(directory, "ls"))) != views:
                raise ValueError("workspace view changed while preparing exit removal")
            snapshot = prune_snapshot(source, receipts)
            data = {"snapshot": snapshot, "panes": sorted(set(config["panes"]) - set(receipts)),
                    "receipts": receipts, "targets": {k: w for k, w in views.items() if w["pane"] in receipts},
                    "view": cli.view_state(directory)}
            atomic_write(journal, json.dumps(data) + "\n")
        else:
            data = json.loads(journal.read_text())

        if cli.view_state(directory) != data["view"]:
            raise ValueError("workspace attachment changed; exit removal retained")
        # Idempotent replay also covers a lost reply or a crash between remote
        # acknowledgement, cache writes and the Kitty close acknowledgement.
        remote_client.publish(directory, data["snapshot"], data["panes"], exit_receipts=data["receipts"])
        remote_client.renew_lease(directory)
        remote_client.ensure_current_revision(directory)
        atomic_write(directory / "workspace.kitty-session", data["snapshot"])
        atomic_write(directory / "panes.json", json.dumps(data["panes"]) + "\n")
        current = current_windows(directory, data["targets"])
        targets = {}
        for key, expected in data["targets"].items():
            if key in current:
                if current[key] != expected or expected["ended"] != "1":
                    raise ValueError("exited pane identity changed; close acknowledgement pending")
                targets[key] = expected
        if targets:
            kitten = str(Path(__file__).with_name("exit_kitten.py"))
            try:
                reply = json.loads(cli.view_query(directory, "kitten", "--match", "id:" + next(iter(targets)),
                                                  kitten, json.dumps(targets)))
                if reply != {"closed": sorted(targets)}:
                    raise ValueError("exit view close was not acknowledged")
            except (OSError, ValueError, subprocess.SubprocessError):
                if not wrappers_gone(targets):
                    raise
            # Kitty schedules child teardown asynchronously. Keep the journal
            # until a subsequent observation confirms every target disappeared.
            current = current_windows(directory, data["targets"])
            if set(data["targets"]) & set(current):
                return 0
        journal.unlink()
        cli.log_pane_event("pane_destroyed", sorted(data["receipts"]))
        return len(data["receipts"])
