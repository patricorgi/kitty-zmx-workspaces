"""Demo replacement safety with private databases and mocked process/SSH APIs."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kzw.remote import Store
from workspace_fixtures import layout

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


local = load("demo_reset_local_test", "demo_reset.py")
origin = load("demo_reset_origin_test", "demo_reset_remote.py")
A, B, C, D = ["kzw-" + letter * 32 for letter in "abcd"]


class DemoResetAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.addCleanup(self.store.close)
        self.live = {A, B, C}
        self.killed = []
        self.fail = set()

        def run(args, **kwargs):
            if args[1:] == ["list", "--short"]:
                return subprocess.CompletedProcess(args, 0, "\n".join(sorted(self.live)) + "\n", "")
            self.assertEqual(args[1], "kill")
            self.assertNotEqual(args[2], C, "another workspace's session must never be killed")
            self.killed.append(args[2])
            if args[2] in self.fail:
                return subprocess.CompletedProcess(args, 1, "", "test failure")
            self.live.discard(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        self.run_patch = patch("kzw.remote.subprocess.run", side_effect=run)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        db = self.store.db
        db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", ("Test", 1, layout(A), json.dumps([A])))
        db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", ("Other", 7, layout(C), json.dumps([C])))
        for pane in (B, D):
            db.execute("INSERT INTO reservations VALUES (?,?)", (pane, "Test"))
        db.execute("INSERT INTO staged_layouts VALUES (?,?,?,?)", ("Test", 1, layout(A, B, D), json.dumps([A, B, D])))
        self.request = {"name": "Test", "revision": 1, "owned": [A, B, D],
                        "token": "f" * 32, "request_id": "e" * 32, "previous_tokens": []}

    def exists(self):
        return self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"]

    def test_explicit_reset_removes_committed_reserved_and_staged_demo_only(self):
        result = origin.destroy(self.store, self.request)
        self.assertTrue(result["removed"])
        self.assertFalse(self.exists())
        self.assertEqual(set(self.killed), {A, B})
        self.assertEqual(self.live, {C})
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Other"})["revision"], 7)
        for table in ("reservations", "staged_layouts", "cleanup", "leases", "workspace_kills"):
            self.assertFalse(self.store.db.execute("SELECT 1 FROM " + table + " WHERE workspace='Test'").fetchone())

    def test_revision_change_prevents_every_kill(self):
        self.request["revision"] = 0
        with self.assertRaisesRegex(ValueError, "revision changed"):
            origin.destroy(self.store, self.request)
        self.assertEqual(self.killed, [])
        self.assertTrue(self.exists())

    def test_changed_claims_prevent_every_kill(self):
        self.request["owned"] = [A]
        with self.assertRaisesRegex(ValueError, "ownership changed"):
            origin.destroy(self.store, self.request)
        self.assertEqual(self.killed, [])

    def test_foreign_layout_reference_prevents_every_kill(self):
        self.store.db.execute("UPDATE workspaces SET panes=?,snapshot=? WHERE name='Other'", (json.dumps([C, B]), layout(C, B)))
        with self.assertRaisesRegex(ValueError, "ownership changed"):
            origin.destroy(self.store, self.request)
        self.assertEqual(self.killed, [])

    def test_active_unknown_lease_is_not_stolen(self):
        self.store.db.execute("INSERT INTO leases VALUES (?,?,?)", ("Test", "1" * 32, time.time() + 100))
        with self.assertRaisesRegex(ValueError, "another client"):
            origin.destroy(self.store, self.request)
        self.assertEqual(self.killed, [])

    def test_exact_known_local_lease_can_be_revoked_for_explicit_reset(self):
        self.store.db.execute("INSERT INTO leases VALUES (?,?,?)", ("Test", "1" * 32, time.time() + 100))
        self.request["previous_tokens"] = ["1" * 32]
        self.assertTrue(origin.destroy(self.store, self.request)["removed"])

    def test_failed_inventory_does_not_cancel_reservations_or_kill(self):
        with patch.object(self.store, "read_inventory", side_effect=OSError("query failed")):
            with self.assertRaises(OSError):
                origin.destroy(self.store, self.request)
        self.assertEqual(self.killed, [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM reservations WHERE workspace='Test'").fetchone()[0], 2)

    def test_failed_kill_retains_durable_cleanup_and_can_resume(self):
        self.fail.add(B)
        with self.assertRaisesRegex(ValueError, "durable intent"):
            origin.destroy(self.store, self.request)
        self.assertTrue(self.exists())
        self.assertTrue(self.store.db.execute("SELECT 1 FROM workspace_kills WHERE workspace='Test'").fetchone())
        self.fail.clear()
        state = origin.inspect(self.store, "Test")
        resumed = {**self.request, "revision": state["revision"], "owned": state["owned"]}
        self.assertTrue(origin.destroy(self.store, resumed)["removed"])
        self.assertFalse(self.exists())
        self.assertEqual(self.live, {C})

    def test_absent_workspace_with_new_unconfirmed_expected_sessions_is_not_success(self):
        self.store.db.execute("DELETE FROM workspaces WHERE name='Test'")
        with self.assertRaisesRegex(ValueError, "disappeared"):
            origin.destroy(self.store, self.request)
        self.assertEqual(self.killed, [])

    def test_absent_empty_target_is_idempotent(self):
        self.store.db.execute("DELETE FROM workspaces WHERE name='Test'")
        self.request["owned"] = []
        self.assertTrue(origin.destroy(self.store, self.request)["removed"])
        self.assertEqual(self.killed, [])


class DemoResetLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache, self.evidence = self.root / "cache/Test", self.root / "run"
        self.cache.mkdir(parents=True)
        self.evidence.mkdir()
        (self.cache / "remote.json").write_text(json.dumps({"workspace": "Test", "host": "origin", "panes": [A]}))
        self.reset = object.__new__(local.Reset)
        self.reset.name, self.reset.host, self.reset.directory = "Test", "origin", self.cache
        self.reset.run_dir, self.reset.env = self.evidence, {"KITTY_LISTEN_ON": "unix:source"}
        self.reset.progress = {"closed_views": [], "stopped_supervisors": []}
        self.before = {"exists": True, "revision": 1, "owned": [A], "inventory": {"available": True, "sessions": [
            {"id": A, "presence": "live", "references": [{"workspace": "Test"}]}]}}
        self.reset.remote = Mock(side_effect=[self.before, self.before, {"removed": True, "sessions": [A]}])
        self.reset.raw = Mock(return_value="[]")
        self.reset.stop_supervisors = Mock()

    def window(self, name="Test", host="origin", pid=11):
        return {"id": 4, "pid": pid, "user_vars": {"kzw_workspace": name, "kzw_host": host,
                "kzw_directory": str(self.cache), "kzw_pane": A}}

    def test_success_archives_cache_and_journals_instead_of_recursively_deleting(self):
        (self.cache / "pending-create.json").write_text(json.dumps({"pane": A}))
        self.assertTrue(self.reset.execute()["removed"])
        self.assertFalse(self.cache.exists())
        self.assertTrue((self.evidence / "previous-workspace/pending-create.json").is_file())
        self.assertEqual(self.reset.remote.call_args_list[-1].args, ("destroy",))
        self.assertEqual(self.reset.remote.call_args_list[-1].kwargs["owned"], [A])

    def test_failed_removal_preserves_local_cache_and_reset_intent(self):
        self.reset.remote.side_effect = [self.before, self.before, OSError("SSH lost")]
        with self.assertRaises(OSError):
            self.reset.execute()
        self.assertTrue((self.cache / "demo-reset.intent.json").is_file())
        self.assertFalse((self.evidence / "previous-workspace").exists())

    def test_changed_remote_revision_blocks_destruction_after_quiescing(self):
        self.reset.remote.side_effect = [self.before, {**self.before, "revision": 2}]
        with self.assertRaisesRegex(local.ResetError, "changed while"):
            self.reset.execute()
        self.assertEqual(self.reset.remote.call_count, 2)
        self.assertTrue(self.cache.exists())

    def test_failed_inventory_does_not_close_views_or_stop_processes(self):
        self.reset.remote.side_effect = [{**self.before, "inventory": {"available": False}}]
        with self.assertRaises(local.ResetError):
            self.reset.execute()
        self.reset.raw.assert_not_called()
        self.reset.stop_supervisors.assert_not_called()

    def test_foreign_cached_reference_blocks_reset(self):
        self.before["owned"] = []
        self.before["inventory"]["sessions"][0]["references"] = [{"workspace": "Other"}]
        with self.assertRaisesRegex(local.ResetError, "another owner"):
            self.reset.execute()
        self.reset.stop_supervisors.assert_not_called()

    def test_view_selection_ignores_other_workspaces(self):
        self.reset.raw.return_value = json.dumps([{"id": 1, "tabs": [{"windows": [self.window(), self.window(name="Other")]}]}])
        selected = self.reset.views({A})
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["pane"], A)

    def test_regular_shared_socket_marker_is_not_used_as_a_socket(self):
        (self.cache / "kitty.sock").touch()
        (self.cache / "view.json").write_text(json.dumps({"address": "unix:source"}))
        self.reset.views({A})
        self.reset.raw.assert_called_once_with("unix:source", "ls")

    def test_running_inside_target_demo_is_rejected_before_closing(self):
        self.reset.env["KITTY_WINDOW_ID"] = "4"
        self.reset.raw.return_value = json.dumps([{"id": 1, "tabs": [{"windows": [self.window()]}]}])
        with self.assertRaisesRegex(local.ResetError, "outside the demo"):
            self.reset.views({A})

    def test_changed_pane_pid_prevents_close(self):
        target = {"address": "unix:source", "os_id": 1, "id": 4, "pid": 10, "pane": A}
        self.reset.raw.return_value = json.dumps([{"id": 1, "tabs": [{"windows": [self.window(pid=11)]}]}])
        with self.assertRaisesRegex(local.ResetError, "identity changed"):
            self.reset.close_views([target])
        self.assertEqual(self.reset.raw.call_count, 1)

    def test_only_exact_supervisor_name_host_and_creation_identity_may_be_stopped(self):
        (self.cache / "lock").touch()
        # Restore the real method that setUp normally mocks.
        self.reset.stop_supervisors = local.Reset.stop_supervisors.__get__(self.reset)
        self.reset.process_identity = Mock(return_value="Thu Nov 12 16:00:00 2026 /bin/python -m kzw.cli attach Test --host origin")
        with patch.object(local.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "101\n", "")), \
                patch.object(local.os, "kill") as kill:
            self.reset.stop_supervisors()
        kill.assert_called_once_with(101, signal.SIGTERM)

    def test_similarly_named_supervisor_is_not_terminated(self):
        (self.cache / "lock").touch()
        self.reset.stop_supervisors = local.Reset.stop_supervisors.__get__(self.reset)
        self.reset.process_identity = Mock(return_value="Thu Nov 12 16:00:00 2026 /bin/python -m kzw.cli attach Test2 --host origin")
        with patch.object(local.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "101\n", "")), \
                patch.object(local.os, "kill") as kill:
            with self.assertRaises(local.ResetError):
                self.reset.stop_supervisors()
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
