import copy
import json
import os
from pathlib import Path
import runpy
import select
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kzw import cli, exits, remote_client
from kzw.pane import ssh_diagnostic_visible
from kzw.layout_prune import prune_snapshot
from kzw.remote import Store, response, zmx_exit_receipt
from test_layout_prune import snapshot

A, B, C = ("kzw-" + c * 32 for c in "abc")


class ExitKittenTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(select, "kqueue"), "requires kqueue")
    def test_existing_change_file_wakes_supervisor_on_touch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watcher.changed"
            with cli.watch_workspace_changes(path) as wait:
                old = path.stat().st_mtime_ns
                os.utime(path, ns=(old - 1_000_000_000, old - 1_000_000_000))
                self.assertTrue(wait(1))

    def test_only_informational_ssh_teardown_is_hidden(self):
        self.assertFalse(ssh_diagnostic_visible("Shared connection to lhcb-login closed.\r\n"))
        self.assertTrue(ssh_diagnostic_visible("Connection to lhcb-login reset by peer\r\n"))
        self.assertTrue(ssh_diagnostic_visible("Shared connection to lhcb-login closed unexpectedly.\n"))

    def test_exact_pid_matches_kitty_listing_including_launch_wrappers(self):
        window = SimpleNamespace(id=17, os_window_id=2, child=SimpleNamespace(pid=40, root_pid=41),
                                 user_vars={"kzw_pane": A, "kzw_attach_ended": "1"})
        for version, pid, wrong_pid in (((0, 49, 2), 40, 41), ((0, 51, 0), 41, 40)):
            with self.subTest(version=version), patch.dict(sys.modules, {
                "kittens.tui.handler": SimpleNamespace(result_handler=lambda **kwargs: lambda fn: fn),
                "kitty.constants": SimpleNamespace(version=version),
            }):
                handle = runpy.run_path(str(Path(exits.__file__).with_name("exit_kitten.py")))["handle_result"]
                boss = SimpleNamespace(window_id_map={17: window}, mark_window_for_close=Mock())
                target = {"id": 17, "pid": wrong_pid, "os_window_id": 2, "pane": A, "ended": "1"}
                with self.assertRaisesRegex(ValueError, "pane changed"):
                    handle(["", json.dumps({"17": target})], None, 17, boss)
                boss.mark_window_for_close.assert_not_called()
                target["pid"] = pid
                self.assertEqual(json.loads(handle(["", json.dumps({"17": target})], None, 17, boss)),
                                 {"closed": ["17"]})
                boss.mark_window_for_close.assert_called_once_with(window)


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
        if args[3].endswith("/scoped_session_kitten.py"):
            self.assertEqual(args[-2:], ("5", "--with-tree"))
            export = "\n".join(line for line in self.source.splitlines() if not line.startswith("# kzw-focus ")) + "\n"
            return json.dumps({"os_window_id": 5, "tree": self.tree, "session": export})
        targets = json.loads(args[-1])
        # The durable plan must exist, but the view must not wait for publication.
        plan = json.loads((directory / "pending-exit.json").read_text())
        for target in targets.values():
            self.assertIn(target["pane"], plan["receipts"])
            self.assertNotIn(target["pane"], plan["panes"])
            self.assertIn(target["pane"], json.loads((directory / "panes.json").read_text()))
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

    def test_zmx_1_0_1_exit_requires_ordered_suffix_and_absent_pid(self):
        self.version = "1.0.1"
        self.ended(B, evidence=False)
        log = self.zmx / "logs" / (B + ".log")

        def suffix(pid):
            lines = ("shell exited pty_fd=6", "daemon loop shutdown", f"deleting socket file session={B}",
                     f"kill received session={B}", f"shutting down daemon session={B}",
                     f"sending SIGHUP session={B} pid={pid}")
            return "".join(f"[1795048227] [info] (default): {line}\n" for line in lines)

        log.write_text(lifecycle(B))
        self.assertEqual(self.sync(), 0)
        log.write_text(suffix(os.getpid()))
        self.assertEqual(self.sync(), 0)
        log.write_text(suffix(2147483647))
        receipt = self.store.exit_receipts({B})[B]
        self.assertEqual(receipt["kind"], "zmx-1.0.1-reaped")
        self.assertEqual(receipt["exited_at_ms"], 1795048227000)
        self.assertNotIn("reaped_at_ms", receipt)
        self.assertEqual(self.sync(), 1)

    def disappear(self, pane):
        tab = self.tree[0]["tabs"][0]
        tab["windows"] = [w for w in tab["windows"] if w["user_vars"]["kzw_pane"] != pane]
        for index, window in enumerate(tab["windows"]):
            window["is_active"] = index == 0
        self.source = prune_snapshot(self.source, {pane})

    def test_vanished_confirmed_exit_backs_up_layout_and_never_closes_a_view(self):
        self.ended(B)
        self.disappear(B)
        previous = (self.directory / "workspace.kitty-session").read_text()
        revision = self.request({"method": "workspace.get", "workspace": "Test"})["revision"]
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.request({"method": "workspace.get", "workspace": "Test"})["panes"], [A, C])
        self.assertEqual((self.directory / "exit-backups" / f"revision-{revision}.kitty-session").read_text(), previous)
        self.assertFalse(self.closes)
        self.assertFalse((self.directory / "pending-exit.json").exists())

    def test_vanished_unproven_exit_blocks_the_whole_update(self):
        self.ended(B)
        self.disappear(C)  # Still live, despite being absent from Kitty.
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.request({"method": "workspace.get", "workspace": "Test"})["panes"], [A, B, C])
        self.assertFalse(self.closes)
        self.assertFalse((self.directory / "exit-backups").exists())

    def test_vanished_exit_replays_after_lost_commit_reply(self):
        self.ended(B)
        self.disappear(B)
        attempts = []

        def call(host, req):
            result = self.request(req)
            if req["method"] == "panes.exited":
                attempts.append(copy.deepcopy(req))
                if len(attempts) == 1:
                    raise OSError("lost commit reply")
            return result

        with patch("kzw.remote_client.call", side_effect=call):
            with self.assertRaisesRegex(OSError, "lost commit reply"):
                self.sync()
            self.assertTrue((self.directory / "pending-exit.json").exists())
            self.assertEqual(self.sync(), 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertFalse(self.closes)

    def test_vanished_exit_is_reconciled_before_managed_creation_capture(self):
        self.ended(B)
        self.disappear(B)

        def capture(directory, executable, *, before_tree):
            self.assertEqual(json.loads((directory / "panes.json").read_text()), [A, C])
            raise ValueError("stop before launching")

        which = shutil.which
        with patch("kzw.cli.state_dir", return_value=self.directory), \
             patch("kzw.cli.shutil.which", side_effect=lambda name, **kw: "/bin/kzw-pane" if name == "kzw-pane"
                   else which(name, **kw)), \
             patch("kzw.cli.capture_tree_locked", side_effect=capture) as check, \
             self.assertRaisesRegex(ValueError, "stop before launching"):
            cli.create_pane("Test", "tab", 1, A, "")
        check.assert_called_once()

    def test_positive_exit_closes_exact_view_before_publish_without_kill(self):
        self.ended(B)
        self.requests.clear()
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
        self.assertEqual([r["method"] for r in self.requests if r["method"] in ("workspace.put", "panes.exited")],
                         ["panes.exited"])

    def test_exit_uses_latest_atomic_layout_without_intermediate_publication(self):
        self.ended(B)
        self.source = self.source.replace("new_tab Test", "new_tab Latest")
        self.requests.clear()
        self.assertEqual(self.sync(), 1)
        self.assertIn("new_tab Latest", (self.directory / "workspace.kitty-session").read_text())
        self.assertNotIn("workspace.put", [r["method"] for r in self.requests])

    def test_atomic_capture_rejects_changed_or_incomplete_view_without_committing(self):
        self.ended(B)
        initial = self.request({"method": "workspace.get", "workspace": "Test"})
        for mode in ("wrong scope", "replaced wrapper", "reconnected", "changed host", "unmanaged", "missing launch", "missing focus"):
            with self.subTest(mode=mode):
                def invalid(directory, *args):
                    result = self.kitty(directory, *args)
                    if args[0] == "kitten" and args[3].endswith("/scoped_session_kitten.py"):
                        data = json.loads(result)
                        ow = data["tree"][0]
                        window = ow["tabs"][0]["windows"][1]
                        if mode == "wrong scope":
                            ow["id"] += 1
                        elif mode == "replaced wrapper":
                            window["pid"] += 1
                        elif mode == "reconnected":
                            window["user_vars"]["kzw_attach_ended"] = "0"
                        elif mode == "changed host":
                            window["user_vars"]["kzw_host"] = "other-host"
                        elif mode == "unmanaged":
                            window["user_vars"] = {}
                        elif mode == "missing launch":
                            data["session"] = "new_tab Empty\n"
                        else:
                            window["is_active"] = False
                        return json.dumps(data)
                    return result
                with patch("kzw.cli.view_query", side_effect=invalid), self.assertRaises(ValueError):
                    self.sync()
                self.assertEqual(self.request({"method": "workspace.get", "workspace": "Test"}), initial)
                self.assertEqual((self.directory / "workspace.kitty-session").read_text(), self.source)
                self.assertFalse((self.directory / "pending-exit.json").exists())
                self.assertFalse(self.closes)

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

    def test_transport_end_preserves_live_session_and_prunes_only_proven_exit(self):
        self.assertTrue(exits.record_transport_end(self.directory, B))
        self.assertTrue(exits.record_transport_end(self.directory, C))
        self.tree[0]["tabs"][0]["windows"] = self.tree[0]["tabs"][0]["windows"][:1]
        self.assertEqual(self.sync(), 0)
        self.assertEqual((self.directory / "workspace.kitty-session").read_text(), self.source)
        self.ended(B)
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.request({"method": "workspace.get", "workspace": "Test"})["panes"], [A, C])
        self.assertEqual(self.closes, [])

    def test_transport_end_can_reconcile_after_last_view_disappears(self):
        for pane in (A, B, C):
            self.assertTrue(exits.record_transport_end(self.directory, pane))
        self.ended(A, B, C)
        self.tree = []
        self.assertEqual(exits.synchronize(self.directory, "/bin/kzw-pane", tree=[]), 3)
        self.assertEqual((self.directory / "workspace.kitty-session").read_text(), cli.EMPTY_SNAPSHOT)
        self.assertEqual(self.closes, [])

    def test_coordinator_state_override_does_not_move_zmx_logs(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": "/isolated-coordinator",
                                     "KZW_ORIGIN_ZMX_STATE_HOME": "/origin-state"}):
            self.assertEqual(self.store.zmx_environment()["XDG_STATE_HOME"], "/origin-state")
            os.environ["KZW_ORIGIN_ZMX_STATE_HOME"] = ""
            self.assertNotIn("XDG_STATE_HOME", self.store.zmx_environment())

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

    def test_lost_commit_reply_replays_same_operation_after_closing(self):
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
            self.assertEqual(len(self.closes), 1)
            self.assertTrue((self.directory / "pending-exit.json").exists())
            (self.zmx / "logs" / (B + ".log")).unlink()  # Replay uses the committed receipt.
            with self.assertRaisesRegex(ValueError, "exit"):
                cli.capture(self.directory, "/bin/kzw-pane")
            self.assertEqual(self.sync(), 1)
        attempts = [r for r in self.requests if r["method"] == "panes.exited"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(len(self.closes), 1)

    def test_lost_view_close_ack_is_recoverable_without_reclosing(self):
        self.ended(B)

        def lost(directory, *args):
            result = self.kitty(directory, *args)
            if args[0] == "kitten" and args[3].endswith("/exit_kitten.py"):
                raise OSError("Kitty reply lost")
            return result

        with patch("kzw.cli.view_query", side_effect=lost), self.assertRaises(OSError):
            self.sync()
        revision = json.loads((self.directory / "remote.json").read_text())["revision"]
        self.assertEqual(self.sync(), 1)
        self.assertEqual(json.loads((self.directory / "remote.json").read_text())["revision"], revision + 1)
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
                   if a[0] == "kitten" and a[3].endswith("/exit_kitten.py") else self.kitty(d, *a)), self.assertRaises(OSError):
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
        self.assertEqual(len(self.closes), 1)
        self.assertEqual(self.sync(), 1)
        self.assertEqual(len(self.closes), 1)

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

    def test_stale_revision_and_foreign_owner_cannot_replay_view_close(self):
        self.ended(B)
        with patch("kzw.cli.view_query", side_effect=lambda d, *a: (_ for _ in ()).throw(OSError("close failed"))
                   if a[0] == "kitten" and a[3].endswith("/exit_kitten.py") else self.kitty(d, *a)), self.assertRaises(OSError):
            self.sync()
        self.store.db.execute("UPDATE workspaces SET revision=revision+1")
        with self.assertRaisesRegex(ValueError, "revision"):
            self.sync()
        self.store.db.execute("UPDATE leases SET token=?", ("b" * 32,))
        with self.assertRaisesRegex(ValueError, "lease"):
            self.sync()
        self.assertFalse(self.closes)

    def test_final_kitty_disappearance_can_publish_after_wrapper_exit(self):
        self.ended(A, B, C)
        closed = []

        def disappearing(directory, *args):
            if closed:
                raise OSError("Kitty has exited")
            result = self.kitty(directory, *args)
            if args[0] == "kitten" and args[3].endswith("/exit_kitten.py"):
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
