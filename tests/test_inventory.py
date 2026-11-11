"""Authority-boundary regressions: inventory is not ownership or a layout edit."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kzw.cli import main
from kzw.remote import Conflict, Store
from workspace_fixtures import layout

A, B, C = ("kzw-" + letter * 32 for letter in "abc")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "server.sqlite"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.live = [A, B, "ordinary-session"]
        self.run = patch("kzw.remote.subprocess.run", side_effect=lambda *a, **kw:
                         subprocess.CompletedProcess(a, 0, "\n".join(self.live) + ("\n" if self.live else ""), ""))
        self.run.start()
        self.addCleanup(self.run.stop)
        self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 0,
                             "request_id": "create", "snapshot": layout(A, B), "panes": [A, B]})

    def test_full_inventory_tracks_layout_changes_pending_work_and_unassigned_sessions(self):
        token = "f" * 32
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})
        self.store.dispatch({"method": "pane.reserve", "workspace": "Test", "revision": 1,
                             "request_id": "reserve", "pane": C, "lease_token": token})
        self.store.dispatch({"method": "workspace.stage", "workspace": "Test", "revision": 1,
                             "snapshot": layout(B, A, C), "panes": [A, B, C], "lease_token": token})
        state = self.store.dispatch({"method": "session.inventory"})
        by_id = {s["id"]: s for s in state["sessions"]}
        self.assertEqual(set(by_id), {A, B, C, "ordinary-session"})
        self.assertEqual((by_id[C]["presence"], by_id[C]["workspace"]), ("missing", "Test"))
        self.assertEqual({r["role"] for r in by_id[C]["references"]}, {"reserved", "staged"})
        self.assertEqual(by_id["ordinary-session"]["assignment"], "unassigned")
        self.assertEqual(state["workspaces"]["Test"]["snapshot"], layout(A, B))
        self.live.append(C)
        self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 1,
                             "request_id": "save", "snapshot": layout(B, A, C), "panes": [A, B, C], "lease_token": token})
        state = self.store.inventory()
        self.assertEqual(state["workspaces"]["Test"]["revision"], 2)
        by_id = {s["id"]: s for s in state["sessions"]}
        self.assertEqual(by_id[A]["references"][0]["location"]["pane"], 2)
        self.assertEqual([r["role"] for r in by_id[C]["references"]], ["layout"])
        self.store.dispatch({"method": "panes.close", "workspace": "Test", "revision": 2,
                             "request_id": "close", "snapshot": layout(A, C), "panes": [A, C],
                             "remove": [B], "lease_token": token})
        by_id = {s["id"]: s for s in self.store.inventory()["sessions"]}
        self.assertEqual((by_id[B]["presence"], by_id[B]["references"]),
                         ("live", [{"workspace": "Test", "role": "closing"}]))
        self.store.dispatch({"method": "workspace.lease.release", "workspace": "Test", "token": token})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.inventory()["workspaces"]["Test"]["snapshot"], layout(A, C))

    def test_absence_failures_and_origin_changes_preserve_authoritative_layout(self):
        original = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.live = [A, "ordinary-session"]
        state = self.store.inventory()
        self.assertEqual({s["id"]: s["presence"] for s in state["sessions"]},
                         {A: "live", B: "missing", "ordinary-session": "live"})
        sample = state["observation"]
        for failure in (OSError("offline"), subprocess.TimeoutExpired("zmx", 10),
                        subprocess.CalledProcessError(1, "zmx"),
                        subprocess.CompletedProcess([], 0, A + "\n" + A + "\n", "")):
            kwargs = {"return_value": failure} if isinstance(failure, subprocess.CompletedProcess) else {"side_effect": failure}
            with self.subTest(failure=failure), patch("kzw.remote.subprocess.run", **kwargs):
                failed = self.store.inventory()
                self.assertFalse(failed["available"])
                self.assertEqual(failed["observation"], sample)
                self.assertEqual({s["presence"] for s in failed["sessions"]}, {"unknown"})
                self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"}), original)
        self.store.close()
        self.store = Store(self.path)
        with patch.dict(os.environ, {"ZMX_DIR": str(Path(self.temp.name) / "wrong-namespace")}), \
             patch("kzw.remote.subprocess.run") as run:
            failed = self.store.inventory()
            self.assertFalse(failed["available"])
            self.assertIn("origin changed", failed["error"])
            run.assert_not_called()
        self.store.observe_detached()
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"}), original)
        # The CLI exposes unknown as JSON and a nonzero exit, rather than an empty list.
        with patch("sys.argv", ["kzw", "inventory", "--host", "origin", "--json"]), \
             patch("kzw.cli.remote_client.call", return_value=failed) as rpc, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(), 1)
        self.assertFalse(json.loads(output.getvalue())["available"])
        rpc.assert_called_once_with("origin", {"method": "session.inventory"})

    def test_server_rejects_inconsistent_layouts_and_stale_ui_without_state_changes(self):
        original = self.store.dispatch({"method": "workspace.sync", "workspace": "Test"})
        bad_layouts = [
            layout(A),  # claimed but undisplayed B
            layout(A, A),
            layout(A, B).replace("--var=kzw_pane=" + A, "--var=kzw_pane=" + B),
            layout(A, B).replace('"id": 2', '"id": 1'),
            layout(A, B).replace("KZW_ORIGIN", "other"),
            layout(A, B) + "launch\t/bin/sh\n",
            layout(A, B) + '# kzw-focus {"pane":"' + C + '"}\n',
            layout(A, B) + 'set_layout_state {"all_windows":{"window_groups":[{"id":1,"window_ids":[1]}]}}\n',
        ]
        for index, snapshot in enumerate(bad_layouts):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 1,
                                     "request_id": str(index), "snapshot": snapshot, "panes": [A, B]})
            self.assertEqual(self.store.dispatch({"method": "workspace.sync", "workspace": "Test"}), original)
        for method, panes, extra in (("workspace.put", [A], {}),
                                     ("workspace.prune", [A], {"remove": [B]}),
                                     ("workspace.reconcile", [A], {"remove": [B]})):
            with self.subTest(method=method), self.assertRaises(ValueError):
                self.store.dispatch({"method": method, "workspace": "Test", "revision": 1,
                                     "request_id": method, "snapshot": layout(*panes), "panes": panes, **extra})
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": "f" * 32})
        with self.assertRaises(Conflict):
            self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 1,
                                 "request_id": "stale-ui", "snapshot": layout(B, A), "panes": [A, B]})
        self.assertEqual(self.store.dispatch({"method": "workspace.sync", "workspace": "Test"}), original)
        self.store.dispatch({"method": "pane.reserve", "workspace": "Test", "revision": 1,
                             "request_id": "reserve", "pane": C, "lease_token": "f" * 32})
        before = self.store.dispatch({"method": "workspace.sync", "workspace": "Test"})
        with self.assertRaisesRegex(ValueError, "manifest disagree"):
            self.store.dispatch({"method": "workspace.stage", "workspace": "Test", "revision": 1,
                                 "snapshot": layout(A, B), "panes": [A, B, C], "lease_token": "f" * 32})
        self.assertEqual(self.store.dispatch({"method": "workspace.sync", "workspace": "Test"}), before)

    def test_first_query_failure_is_unknown_and_cli_output_never_claims_empty_success(self):
        fresh = Store(Path(self.temp.name) / "fresh.sqlite")
        self.addCleanup(fresh.close)
        with patch("kzw.remote.subprocess.run", side_effect=OSError("zmx unavailable")):
            result = fresh.inventory()
        self.assertFalse(result["available"])
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["observation"]["generation"], 0)
        self.assertIsNone(result["observation"]["observed_at"])
        self.assertIsNone(result["observation"]["source"])
        self.assertEqual(fresh.db.execute("SELECT count(*) FROM zmx_inventory").fetchone()[0], 0)
        for as_json in (False, True):
            for available in (False, True):
                with self.subTest(json=as_json, available=available):
                    state = self.store.inventory() if available else result
                    argv = ["kzw", "inventory"] + (["--json"] if as_json else [])
                    with patch("sys.argv", argv), patch("kzw.cli.remote_client.call", return_value=state) as rpc, \
                         patch("sys.stdout", new_callable=io.StringIO) as output:
                        self.assertEqual(main(), 0 if available else 1)
                    rpc.assert_called_once_with("", {"method": "session.inventory"})
                    if as_json:
                        self.assertEqual(json.loads(output.getvalue()), state)
                    else:
                        self.assertIn("inventory: available" if available else "inventory: unknown", output.getvalue())
                        self.assertIn("ordinary-session\tlive\tunassigned" if available else "zmx unavailable", output.getvalue())

    def test_provenance_pins_each_origin_dimension_and_sanitizes_nested_context(self):
        sample = self.store.inventory()["observation"]
        source = sample["source"]
        for target, value in (("socket.gethostname", source["host"] + "-different"),
                              ("os.getuid", source["uid"] + 1),
                              ("shutil.which", "/other/bin/zmx")):
            with self.subTest(dimension=target), patch("kzw.remote." + target, return_value=value), \
                 patch("kzw.remote.subprocess.run") as run:
                result = self.store.inventory()
                self.assertFalse(result["available"])
                self.assertEqual(result["observation"], sample)
                self.assertNotEqual(result["query_source"], source)
                run.assert_not_called()
        with patch.dict(os.environ, {"ZMX_SESSION": A, "ZMX_SESSION_PREFIX": "nested-"}), \
             patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, A + "\n", "")) as run:
            self.store.inventory()
        env = run.call_args.kwargs["env"]
        self.assertNotIn("ZMX_SESSION", env)
        self.assertNotIn("ZMX_SESSION_PREFIX", env)
        self.assertEqual(run.call_args.args[0][1:], ["list", "--short"])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        for environment, selector, base in (({"ZMX_DIR": "/z", "XDG_RUNTIME_DIR": "/r", "TMPDIR": "/t"}, "ZMX_DIR", "/z"),
                                             ({"XDG_RUNTIME_DIR": "/r", "TMPDIR": "/t"}, "XDG_RUNTIME_DIR", "/r"),
                                             ({"TMPDIR": "/t"}, "TMPDIR", "/t"), ({}, None, "/tmp")):
            with self.subTest(namespace=selector):
                info = self.store.inventory_source({"PATH": "/bin", **environment})
                self.assertEqual((info["socket_selector"], info["socket_base"]), (selector, base))

    def test_invalid_historical_layout_and_conflicting_claims_remain_inspectable(self):
        self.store.db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", ("Other", 7, "broken historic layout", json.dumps([A])))
        before = self.store.db.execute("SELECT * FROM workspaces ORDER BY name").fetchall()
        result = self.store.inventory()
        self.assertTrue(result["available"])
        by_id = {row["id"]: row for row in result["sessions"]}
        self.assertEqual(by_id[A]["assignment"], "conflict")
        self.assertIsNone(by_id[A]["workspace"])
        self.assertEqual({ref["workspace"] for ref in by_id[A]["references"]}, {"Test", "Other"})
        self.assertIn("layout_error", result["workspaces"]["Other"])
        self.assertEqual(result["workspaces"]["Other"]["snapshot"], "broken historic layout")
        self.assertEqual(self.store.db.execute("SELECT * FROM workspaces ORDER BY name").fetchall(), before)

    def test_presence_events_preserve_exact_source_and_sample_without_layout_changes(self):
        state = self.store.dispatch({"method": "workspace.sync", "workspace": "Test"})
        self.live = [A, "ordinary-session"]
        self.store.dispatch({"method": "workspace.status", "workspace": "Test"})
        self.store.dispatch({"method": "workspace.status", "workspace": "Test"})
        self.live.append(B)
        self.store.dispatch({"method": "workspace.status", "workspace": "Test"})
        events = self.store.dispatch({"method": "workspace.events", "workspace": "Test", "after": state["event_cursor"]})["events"]
        self.assertEqual([(e["kind"], e["data"]["pane"]) for e in events], [("session.missing", B), ("session.live", B)])
        self.assertEqual(events[0]["data"]["listed_sessions"], [A, "ordinary-session"])
        self.assertEqual(set(events[1]["data"]["listed_sessions"]), {A, B, "ordinary-session"})
        self.assertLess(events[0]["data"]["inventory_generation"], events[1]["data"]["inventory_generation"])
        self.assertEqual(events[0]["data"]["source"], events[1]["data"]["source"])
        self.assertIsInstance(events[0]["data"]["observed_at"], float)
        current = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual((current["snapshot"], current["panes"], current["revision"]),
                         (state["snapshot"], state["panes"], state["revision"]))
