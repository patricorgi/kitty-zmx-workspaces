import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kzw import cli, exits, remote_client
from kzw.remote import Store, response, zmx_exit_receipt
from test_layout_prune import snapshot

A, B, C = ("kzw-" + c * 32 for c in "abc")


def lifecycle(pane, pid=2147483647):
    messages = ["shell exited pty_fd=6", "kill received session=" + pane,
                "shutting down daemon session=" + pane, "sending SIGHUP session=" + pane + " pid=" + str(pid),
                "deleting socket file session=" + pane]
    return "".join("[" + str(1000 + i) + "] [info] (default): " + text + "\n" for i, text in enumerate(messages))


class ExitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.zmx = self.root / "zmx"
        (self.zmx / "logs").mkdir(parents=True)
        self.directory = self.root / "Test"
        self.directory.mkdir()
        self.store = Store(self.root / "server" / "state.sqlite")
        self.addCleanup(self.store.close)
        self.live = {A, B, C}
        self.version = "0.5.0"
        self.requests = []
        self.install(patch.dict(os.environ, {"ZMX_DIR": str(self.zmx), "KZW_REMOTE_STATE_HOME": ""}))
        self.install(patch("kzw.remote.subprocess.run", side_effect=self.zmx_call))
        self.install(patch("kzw.remote_client.call", side_effect=lambda host, req: self.request(req)))
        remote_client.create(self.directory, "Test", "")
        self.token = remote_client.acquire_lease(self.directory, "Test", "")
        portable = "\n".join(line + " --host KZW_ORIGIN --existing" if line.startswith("launch ") else line
                              for line in snapshot().splitlines()) + "\n"
        self.source, _ = remote_client.translate(portable, "/bin/kzw-pane", "", False)
        remote_client.publish(self.directory, self.source, [A, B, C])
        (self.directory / "workspace.kitty-session").write_text(self.source)
        (self.directory / "panes.json").write_text(json.dumps([A, B, C]))
        self.tree = [{"id": 5, "last_focused": True, "tabs": [{"id": 6, "is_active": True, "windows": [
            {"id": i, "pid": 100 + i, "is_active": i == 2,
             "user_vars": {"kzw_pane": pane, "kzw_host": "", "kzw_attach_ended": "0"}}
            for i, pane in enumerate((A, B, C), 1)]}]}]
        self.closes = []
        self.install(patch("kzw.cli.view_query", side_effect=self.kitty))
        self.install(patch("kzw.exits.wrappers_gone", return_value=False))

    def install(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def request(self, req):
        self.requests.append(copy.deepcopy(req))
        return remote_client.decode_response(json.dumps(response(self.store, json.dumps(req))))

    def zmx_call(self, args, **kwargs):
        if args[1:] == ["list", "--short"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(sorted(self.live)) + ("\n" if self.live else ""))
        self.assertEqual(args[1:], ["version"])
        return subprocess.CompletedProcess(args, 0, "zmx\t" + self.version + "\nsocket_dir\t" + str(self.zmx) +
                                           "\nlog_dir\t" + str(self.zmx / "logs") + "\n")

    def kitty(self, directory, *args):
        if args == ("ls",):
            return json.dumps(self.tree)
        if args == ("ls", "--output-format=session"):
            return "\n".join(line for line in self.source.splitlines() if not line.startswith("# kzw-focus ")) + "\n"
        self.assertEqual(args[0], "kitten")
        targets = json.loads(args[-1])
        # At the instant of UI mutation both the authoritative and cached
        # layouts must already exclude the exact ended sessions.
        committed = self.request({"method": "workspace.get", "workspace": "Test"})
        for target in targets.values():
            self.assertNotIn(target["pane"], committed["panes"])
            self.assertNotIn(target["pane"], json.loads((directory / "panes.json").read_text()))
        self.closes.append(targets)
        for ow in self.tree:
            for tab in ow["tabs"]:
                tab["windows"] = [w for w in tab["windows"] if str(w["id"]) not in targets]
        if not any(tab["windows"] for ow in self.tree for tab in ow["tabs"]):
            self.tree = []
        return json.dumps({"closed": sorted(targets)})

    def ended(self, *panes, evidence=True):
        for pane in panes:
            self.live.discard(pane)
            if evidence:
                (self.zmx / "logs" / (pane + ".log")).write_text(lifecycle(pane))
        for ow in self.tree:
            for tab in ow["tabs"]:
                for w in tab["windows"]:
                    if w["user_vars"]["kzw_pane"] in panes:
                        w["user_vars"]["kzw_attach_ended"] = "1"

    def sync(self):
        return cli.synchronize_exits(self.directory, "/bin/kzw-pane")

    def test_positive_exit_commits_before_exact_view_close_without_kill(self):
        self.ended(B)
        self.assertEqual(self.sync(), 1)
        remote = self.request({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual(remote["panes"], [A, C])
        self.assertEqual(len(self.closes), 1)
        self.assertEqual(set(self.closes[0]), {"2"})
        self.assertIn(A, (self.directory / "workspace.kitty-session").read_text())
        self.assertNotIn(B, (self.directory / "workspace.kitty-session").read_text())
        self.assertFalse((self.directory / "pending-exit.json").exists())
        self.assertEqual(self.store.db.execute("SELECT * FROM cleanup").fetchall(), [])
        event = json.loads(self.store.db.execute("SELECT data FROM workspace_events ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertEqual(event["reason"], "confirmed_exit")

    def test_multiple_and_final_exits_save_empty_without_closing_unrelated_views(self):
        self.ended(A, B, C)
        self.assertEqual(self.sync(), 3)
        self.assertEqual((self.directory / "workspace.kitty-session").read_text(), "# kzw-empty-workspace\n")
        self.assertEqual(json.loads((self.directory / "panes.json").read_text()), [])
        self.assertEqual(self.sync(), 0)

    def test_observations_without_complete_origin_evidence_preserve_layout(self):
        self.ended(B, evidence=False)
        for tail in ("", "logout\n", lifecycle(B).splitlines()[0] + "\n",
                     lifecycle(B).replace("shell exited", "client disconnected"),
                     lifecycle(B) + "[2000] [info] (default): pty spawned session=" + B + " pid=42\n",
                     lifecycle(B, os.getpid()), lifecycle(A)):
            with self.subTest(tail=tail[:50]):
                (self.zmx / "logs" / (B + ".log")).write_text(tail)
                self.assertEqual(self.sync(), 0)
        self.assertEqual((self.directory / "workspace.kitty-session").read_text(), self.source)
        self.assertEqual(self.closes, [])

    def test_live_socket_live_inventory_or_unknown_version_veto_old_receipt(self):
        self.ended(B)
        self.live.add(B)
        self.assertEqual(self.sync(), 0)
        self.live.remove(B)
        socket = self.zmx / B
        socket.touch()
        self.assertEqual(self.sync(), 0)
        socket.unlink()
        self.version = "0.6.0"
        self.assertEqual(self.sync(), 0)
        self.assertFalse(self.closes)

    def test_disconnected_live_view_inventory_failure_and_partial_restore(self):
        self.ended(B)
        self.live.add(B)
        self.assertEqual(self.sync(), 0)
        with patch("kzw.remote.subprocess.run", side_effect=OSError("offline")), self.assertRaises(ValueError):
            self.sync()
        self.tree[0]["tabs"][0]["windows"].pop()
        self.assertEqual(self.sync(), 0)
        self.assertFalse(self.closes)

    def test_lost_commit_reply_replays_same_operation_before_closing(self):
        self.ended(B)
        dropped = []

        def lost(host, req):
            result = self.request(req)
            if req["method"] == "panes.exited" and not dropped:
                dropped.append(copy.deepcopy(req))
                raise OSError("reply lost")
            return result

        with patch("kzw.remote_client.call", side_effect=lost):
            with self.assertRaises(OSError):
                self.sync()
            self.assertFalse(self.closes)
            self.assertTrue((self.directory / "pending-exit.json").exists())
            (self.zmx / "logs" / (B + ".log")).unlink()  # Replay uses the committed receipt.
            with self.assertRaisesRegex(ValueError, "exit"):
                cli.capture(self.directory, "/bin/kzw-pane")
            self.assertEqual(self.sync(), 1)
        attempts = [r for r in self.requests if r["method"] == "panes.exited"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])

    def test_lost_view_close_ack_is_recoverable_without_republishing(self):
        self.ended(B)

        def lost(directory, *args):
            result = self.kitty(directory, *args)
            if args[0] == "kitten":
                raise OSError("Kitty reply lost")
            return result

        with patch("kzw.cli.view_query", side_effect=lost), self.assertRaises(OSError):
            self.sync()
        revision = json.loads((self.directory / "remote.json").read_text())["revision"]
        self.assertEqual(self.sync(), 1)
        self.assertEqual(json.loads((self.directory / "remote.json").read_text())["revision"], revision)
        self.assertEqual(len(self.closes), 1)

    def test_server_revalidates_evidence_and_lease_before_layout_removal(self):
        self.ended(B)
        receipt = remote_client.exit_receipts(self.directory, [B])
        desired = exits.prune_snapshot(self.source, [B])
        self.live.add(B)
        with self.assertRaisesRegex(ValueError, "evidence"):
            remote_client.publish(self.directory, desired, [A, C], exit_receipts=receipt)
        self.assertEqual(self.request({"method": "workspace.get", "workspace": "Test"})["panes"], [A, B, C])
        self.live.remove(B)
        self.store.db.execute("UPDATE leases SET token=?", ("b" * 32,))
        with self.assertRaisesRegex(ValueError, "lease"):
            remote_client.publish(self.directory, desired, [A, C], exit_receipts=receipt)

    def test_pending_exit_does_not_close_replaced_or_reconnected_wrapper(self):
        self.ended(B)
        with patch("kzw.cli.view_query", side_effect=lambda d, *a: (_ for _ in ()).throw(OSError("close failed"))
                   if a[0] == "kitten" else self.kitty(d, *a)), self.assertRaises(OSError):
            self.sync()
        self.tree[0]["tabs"][0]["windows"][1]["pid"] += 1
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.sync()
        self.assertFalse(self.closes)
        self.assertTrue((self.directory / "pending-exit.json").exists())

    def test_disk_failure_after_commit_preserves_journal_for_exact_replay(self):
        self.ended(B)
        original = exits.atomic_write

        def fail(path, text):
            if path.name == "panes.json":
                raise OSError("disk full")
            original(path, text)

        with patch("kzw.exits.atomic_write", side_effect=fail), self.assertRaises(OSError):
            self.sync()
        self.assertFalse(self.closes)
        self.assertEqual(self.sync(), 1)

    def test_incomplete_exit_blocks_new_attachment_before_lease_or_view_changes(self):
        journal = self.directory / "pending-exit.json"
        journal.write_text('{"interrupted":true}\n')
        with patch("kzw.cli.state_dir", return_value=self.directory), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.remote_client.acquire_lease") as lease, \
             patch("kzw.cli.remove_view") as remove, self.assertRaisesRegex(ValueError, "exit reconciliation"):
            cli.attach_remote("Test", "")
        lease.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(journal.read_text(), '{"interrupted":true}\n')

    def test_stale_revision_and_foreign_owner_cannot_close_even_after_commit(self):
        self.ended(B)
        with patch("kzw.cli.view_query", side_effect=lambda d, *a: (_ for _ in ()).throw(OSError("close failed"))
                   if a[0] == "kitten" else self.kitty(d, *a)), self.assertRaises(OSError):
            self.sync()
        self.store.db.execute("UPDATE workspaces SET revision=revision+1")
        with self.assertRaisesRegex(ValueError, "revision"):
            self.sync()
        self.store.db.execute("UPDATE leases SET token=?", ("b" * 32,))
        with self.assertRaisesRegex(ValueError, "lease"):
            self.sync()
        self.assertFalse(self.closes)

    def test_final_kitty_disappearance_finishes_only_after_commit_and_wrapper_exit(self):
        self.ended(A, B, C)
        closed = []

        def disappearing(directory, *args):
            if closed:
                raise OSError("Kitty has exited")
            result = self.kitty(directory, *args)
            if args[0] == "kitten":
                closed.append(True)
                raise OSError("last-window reply lost")
            return result

        with patch("kzw.cli.view_query", side_effect=disappearing), \
             patch("kzw.exits.wrappers_gone", side_effect=lambda targets: bool(closed)):
            self.assertEqual(self.sync(), 3)
        self.assertFalse((self.directory / "pending-exit.json").exists())

    def test_latest_log_suffix_survives_earlier_rotation_but_not_split_suffix(self):
        self.ended(B)
        log = self.zmx / "logs" / (B + ".log")
        log.write_text("x" * 20000 + "\n" + lifecycle(B))
        self.assertIsNotNone(zmx_exit_receipt(self.zmx, B))
        log.rename(log.with_suffix(".log.old"))
        log.write_text(lifecycle(B).splitlines()[-1] + "\n")
        self.assertIsNone(zmx_exit_receipt(self.zmx, B))
        log.unlink()
        log.symlink_to(log.with_suffix(".log.old"))
        with self.assertRaises(OSError):
            zmx_exit_receipt(self.zmx, B)
