"""Checks join all inventory entries but never adopt, prune or kill sessions."""

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kzw import cli
from kzw.check import summarize
from kzw.remote import Store
from workspace_fixtures import layout

A, B, C = ("kzw-" + letter * 32 for letter in "abc")


class CheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "server.sqlite")
        self.addCleanup(self.store.close)
        self.live = [A, B, C, "ordinary-session"]
        runner = patch("kzw.remote.subprocess.run", side_effect=lambda *a, **kw:
                       subprocess.CompletedProcess(a, 0, "\n".join(self.live) + ("\n" if self.live else ""), ""))
        self.run = runner.start()
        self.addCleanup(runner.stop)
        self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 0,
                             "request_id": "create", "snapshot": layout(A, B), "panes": [A, B]})

    def check(self, host="origin", as_json=True, inventory=None):
        argv = ["kzw", "check"] + (["--host", host] if host else []) + (["--json"] if as_json else [])
        with patch("sys.argv", argv), patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch("kzw.cli.remote_client.call", side_effect=lambda selected, req:
                   inventory if inventory is not None else self.store.dispatch(req)) as rpc:
            code = cli.main()
        rpc.assert_called_once_with(host, {"method": "session.inventory"})
        text = output.getvalue()
        return code, json.loads(text) if as_json else text

    def metadata(self):
        return {table: self.store.db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                for table in ("workspaces", "reservations", "staged_layouts", "cleanup", "leases", "operations", "workspace_events")}

    def test_reports_all_strays_including_non_kzw_names_without_mutating_ownership(self):
        before = self.metadata()
        self.run.reset_mock()
        code, result = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(result["origin"], "origin")
        self.assertEqual(result["check"]["stray_sessions"], [C, "ordinary-session"])
        self.assertEqual(result["check"]["live_sessions"], 4)
        self.assertEqual(result["check"]["status"], "issues")
        self.assertEqual({s["id"] for s in result["sessions"]}, set(self.live))
        self.assertEqual(result["workspaces"]["Test"]["panes"], [A, B])
        self.assertIsNotNone(result["observation"]["source"])
        self.assertEqual(self.metadata(), before)
        self.assertTrue(all(call.args[0][1:] == ["list", "--short"] for call in self.run.call_args_list))
        _, text = self.check(as_json=False)
        self.assertIn("Stray: 2", text)
        self.assertIn("(unassigned)", text)
        for pane in self.live:
            self.assertEqual(text.count(pane), 1)

    def test_clean_local_check_and_empty_workspace_are_successful(self):
        self.live = [A, B]
        code, result = self.check(host="")
        self.assertEqual((code, result["check"]["status"]), (0, "ok"))
        self.assertEqual(result["check"]["stray_sessions"], [])
        self.store.dispatch({"method": "workspace.put", "workspace": "Empty", "revision": 0,
                             "request_id": "empty", "snapshot": "# kzw-dormant-workspace\n", "panes": []})
        code, text = self.check(host="", as_json=False)
        self.assertEqual(code, 0)
        self.assertIn("Check local: ok", text)
        self.assertIn("workspaces: 2", text)
        self.assertIn("Empty", text)
        self.assertIn("(no session references)", text)

    def test_no_workspaces_or_sessions_is_clean(self):
        self.live = []
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "empty.sqlite")
            try:
                inventory = store.inventory()
            finally:
                store.close()
        code, report = self.check(inventory=inventory)
        self.assertEqual(code, 0)
        self.assertEqual((report["check"]["live_sessions"], report["check"]["workspace_count"]), (0, 0))

    def test_reservations_stages_and_cleanup_prevent_false_strays(self):
        self.live = [A, B]
        token = "f" * 32
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})
        self.store.dispatch({"method": "pane.reserve", "workspace": "Test", "revision": 1,
                             "request_id": "reserve", "pane": C, "lease_token": token})
        self.store.dispatch({"method": "workspace.stage", "workspace": "Test", "revision": 1,
                             "snapshot": layout(A, B, C), "panes": [A, B, C], "lease_token": token})
        for present in (False, True):
            with self.subTest(present=present):
                self.live = [A, B] + ([C] if present else [])
                before = self.metadata()
                code, report = self.check()
                self.assertEqual(code, 0)
                self.assertEqual(report["check"]["stray_sessions"], [])
                self.assertEqual(report["check"]["missing_sessions"], [])
                self.assertEqual(report["check"]["pending_sessions"], [C])
                self.assertEqual(self.metadata(), before)
        self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 1,
                             "request_id": "commit", "snapshot": layout(A, B, C), "panes": [A, B, C], "lease_token": token})
        self.store.dispatch({"method": "panes.close", "workspace": "Test", "revision": 2,
                             "request_id": "close", "snapshot": layout(A, C), "panes": [A, C],
                             "remove": [B], "lease_token": token})
        code, report = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(report["check"]["pending_sessions"], [B])
        self.assertEqual(report["check"]["stray_sessions"], [])

    def test_missing_saved_session_is_reported_without_pruning(self):
        self.live = [A]
        before = self.metadata()
        code, report = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(report["check"]["missing_sessions"], [B])
        self.assertEqual(report["check"]["stray_sessions"], [])
        self.assertEqual(self.metadata(), before)

    def test_conflicts_and_invalid_historical_layouts_remain_inspectable(self):
        self.live = [A, B]
        self.store.db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", ("Other", 7, "broken layout", json.dumps([A])))
        before = self.metadata()
        code, report = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(report["check"]["conflicting_sessions"], [A])
        self.assertIn("Other", report["check"]["invalid_workspaces"])
        self.assertEqual(report["check"]["stray_sessions"], [])
        _, text = self.check(as_json=False)
        self.assertIn("Other,Test", text)
        self.assertIn("Invalid layout Other:", text)
        self.assertEqual(self.metadata(), before)

    def test_failed_inventory_never_labels_cached_entries_as_live_strays(self):
        previous = self.store.inventory()["observation"]
        with patch("kzw.remote.subprocess.run", side_effect=OSError("zmx unavailable")):
            inventory = self.store.inventory()
        for as_json in (True, False):
            with self.subTest(json=as_json):
                code, result = self.check(inventory=inventory, as_json=as_json)
                self.assertEqual(code, 1)
                if as_json:
                    self.assertEqual(result["observation"], previous)
                    self.assertEqual(result["check"]["status"], "incomplete")
                    self.assertIsNone(result["check"]["stray_sessions"])
                    self.assertIsNone(result["check"]["missing_sessions"])
                    self.assertIsNone(result["check"]["live_sessions"])
                else:
                    self.assertIn("Inventory unavailable: zmx unavailable", result)
                    self.assertIn("Stray: unknown", result)
                    self.assertNotIn("Stray: 0", result)

    def test_transport_failure_exits_nonzero_without_clean_report(self):
        with patch("sys.argv", ["kzw", "check", "--host", "origin"]), \
             patch("kzw.cli.remote_client.call", side_effect=OSError("SSH disconnected")), \
             patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(cli.main(), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("SSH disconnected", error.getvalue())

    def test_report_escapes_terminal_controls_and_summary_does_not_modify_inventory(self):
        self.live.append("unassigned\x1b[31m\tname")
        inventory = self.store.inventory()
        saved = json.dumps(inventory, sort_keys=True)
        summarize(inventory)
        self.assertEqual(json.dumps(inventory, sort_keys=True), saved)
        _, text = self.check(as_json=False, inventory=inventory)
        self.assertNotIn("\x1b", text)
        self.assertIn(r"unassigned\u001b[31m\tname", text)
