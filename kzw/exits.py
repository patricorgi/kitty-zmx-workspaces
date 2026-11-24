"""Close views for origin-proven exits and reconcile their layouts durably."""

import json
import os
from pathlib import Path
import subprocess
from contextlib import nullcontext

from . import remote_client
from .core import FOCUS_PREFIX, atomic_write, focused_pane, pane_map, rewrite_export
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
        # A private Kitty may disappear with its last pane before replying.
        if wrappers_gone(targets):
            return {}
        raise


def snapshot_for_exit(directory, executable, tree):
    """Capture the current complete layout and identities in one Kitty callback."""
    from . import cli
    mapping, views = pane_map(tree), windows(tree)
    current, session = cli.scoped_snapshot(directory, tree)
    if pane_map(current) != mapping or windows(current) != views:
        raise ValueError("workspace view changed while preparing exit removal")
    focus = focused_pane(current)
    return FOCUS_PREFIX + json.dumps({"pane": focus}, separators=(",", ":")) + "\n" + rewrite_export(
        session, mapping, executable)


def transport_candidates(directory, saved):
    """Read markers left by older wrappers that closed on transport loss."""
    signals = directory / "transport-ended"
    if signals.is_symlink() or not signals.is_dir():
        return set()
    return {pane for pane in saved if (signals / pane).is_file() and not (signals / pane).is_symlink()}


def synchronize(directory, executable, locked=False, *, tree=None):
    from . import cli
    if not (directory / "remote.json").is_file():
        return 0  # Legacy mixed-origin layouts lack this coordinator contract.
    with nullcontext() if locked else cli.capture_lock(directory):
        journal = directory / "pending-exit.json"
        if journal.is_symlink():
            raise ValueError("unsafe exit journal")
        replay = journal.exists()
        if not replay:
            if any((directory / item).exists() for item in
                   ("pending-create.json", "pending-close.json", "pending-local-kill.json",
                    "native-stage.json", "bootstrap.kitty-session")):
                return 0
            native = directory / "native-pending"
            if native.exists() and (not native.is_dir() or any(native.iterdir())):
                return 0
            config = json.loads((directory / "remote.json").read_text())
            if config.get("pending") or not config.get("panes"):
                return 0
            view = cli.view_state(directory)
            saved = set(config["panes"])
            disconnected = transport_candidates(directory, saved)
            if tree is None:
                tree = json.loads(cli.view_query(directory, "ls"))
            if not tree and disconnected:
                mapping = {}
            elif len(tree) == 1 and (view is None or tree[0].get("id") == view["os_window_id"]):
                mapping = pane_map(tree)
            else:
                return 0  # A vanished view alone is not an exit candidate.
            visible = {p for p, host in mapping.values()}
            if not visible.issubset(saved) or any(host != config["host"] for p, host in mapping.values()):
                return 0  # Unregistered creation or mismatched origin.
            missing = saved - visible
            views = windows(tree)
            candidates = (missing if tree else missing & disconnected) | {
                w["pane"] for w in views.values() if w["ended"] == "1"}
            if not candidates:
                return 0
            receipts = remote_client.exit_receipts(directory, candidates)
            if not receipts or (tree and not (missing - set(receipts)).issubset(disconnected)):
                return 0
            # Already absent leaves are not in the export. Prune only proven
            # exits that still have a view, using the current layout and focus.
            if missing:
                previous = (directory / "workspace.kitty-session").read_text()
                portable, panes = remote_client.translate(previous, "", config["host"], True)
                if set(panes) != saved or remote_client.snapshot_hash(portable, panes) != config["published_hash"]:
                    raise ValueError("saved layout differs from the published revision; exit removal deferred")
                atomic_write(directory / "exit-backups" / f"revision-{config['revision']}.kitty-session", previous)
            if not tree or missing - set(receipts):
                # The live view omits disconnected sessions that must survive.
                snapshot = prune_snapshot(previous, set(receipts))
            else:
                source = snapshot_for_exit(directory, executable, tree)
                present_exits = visible.intersection(receipts)
                snapshot = prune_snapshot(source, present_exits) if present_exits else source
            if cli.view_state(directory) != view:
                raise ValueError("workspace attachment changed during exit capture")
            data = {"snapshot": snapshot, "panes": sorted(saved - set(receipts)),
                    "receipts": receipts, "targets": {k: w for k, w in views.items() if w["pane"] in receipts},
                    "view": view}
            atomic_write(journal, json.dumps(data) + "\n")
        else:
            data = json.loads(journal.read_text())

        if cli.view_state(directory) != data["view"]:
            raise ValueError("workspace attachment changed; exit removal retained")
        if not data.get("view_closed"):
            # The initial atomic capture already checked these identities.
            # Kitty checks again inside its event loop immediately before close.
            current = views if not replay else (
                current_windows(directory, data["targets"]) if data["targets"] else {})
            targets = {}
            for key, expected in data["targets"].items():
                if key in current:
                    if current[key] != expected or expected["ended"] != "1":
                        raise ValueError("exited pane identity changed; close acknowledgement pending")
                    targets[key] = expected
            if targets:
                if replay:
                    # On replay, recheck the fence before acting on older evidence.
                    remote_client.renew_lease(directory)
                    remote_client.ensure_current_revision(directory)
                kitten = str(Path(__file__).with_name("exit_kitten.py"))
                try:
                    reply = json.loads(cli.view_query(directory, "kitten", "--match", "id:" + next(iter(targets)),
                                                      kitten, json.dumps(targets)))
                    if reply != {"closed": sorted(targets)}:
                        raise ValueError("exit view close was not acknowledged")
                except (OSError, ValueError, subprocess.SubprocessError):
                    if not wrappers_gone(targets):
                        raise
                if set(data["targets"]) & set(current_windows(directory, data["targets"])):
                    return 0
            data["view_closed"] = True
            atomic_write(journal, json.dumps(data) + "\n")
        # Once the exited pane is gone, publication can retry without touching
        # Kitty again. The origin still revalidates evidence and ownership.
        remote_client.publish(directory, data["snapshot"], data["panes"], exit_receipts=data["receipts"])
        atomic_write(directory / "workspace.kitty-session", data["snapshot"])
        atomic_write(directory / "panes.json", json.dumps(data["panes"]) + "\n")
        journal.unlink()
        for pane in data["receipts"]:
            (directory / "transport-ended" / pane).unlink(missing_ok=True)
        cli.log_pane_event("pane_destroyed", sorted(data["receipts"]))
        return len(data["receipts"])
