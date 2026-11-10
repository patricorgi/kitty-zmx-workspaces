import subprocess
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kzw.remote import Conflict, Store, response

A = "kzw-" + "a" * 32
B = "kzw-" + "b" * 32


class RemoteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "private" / "state.sqlite")
        self.listing = patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, A + "\n" + B + "\n", ""))
        self.listing.start()

    def tearDown(self):
        self.listing.stop()
        self.store.close()
        self.temp.cleanup()

    def put(self, request_id="one", revision=0, panes=None):
        return self.store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": request_id,
                                    "revision": revision, "snapshot": "layout", "panes": panes or [A, B]})

    def test_local_coordinator_uses_private_subdirectory(self):
        from kzw.remote import state_root
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name, "KZW_LOCAL_COORDINATOR": "1"}):
            self.assertEqual(state_root(), Path(self.temp.name) / "kzw" / "coordinator")
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name, "KZW_LOCAL_COORDINATOR": ""}):
            self.assertEqual(state_root(), Path(self.temp.name) / "kzw")

    def test_compare_and_swap_and_idempotency(self):
        self.assertEqual(self.put(), self.put())
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["revision"], 1)
        with self.assertRaises(Conflict):
            self.put("one", revision=1)
        with self.assertRaises(Conflict):
            self.put("two", revision=0)

    def test_rename_preserves_panes_fences_old_name_and_replays(self):
        self.put(panes=[A])
        request = {"method": "workspace.rename", "workspace": "Test", "destination": "Renamed",
                   "revision": 1, "request_id": "rename-one"}
        result = self.store.dispatch(request)
        self.assertEqual(result["panes"], [A])
        self.assertEqual(self.store.dispatch(request), result)
        self.assertFalse(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])
        new = self.store.dispatch({"method": "workspace.get", "workspace": "Renamed"})
        self.assertEqual((new["snapshot"], new["panes"]), ("layout", [A]))
        self.assertGreater(new["revision"], 1)
        self.assertEqual(self.store.db.execute("SELECT workspace FROM observed_sessions WHERE pane=?", (A,)).fetchone()[0], "Renamed")
        with self.assertRaises(Conflict):
            self.put("stale", revision=1, panes=[A])
        self.assertTrue(any(event["kind"] == "workspace.renamed" for event in
                            self.store.dispatch({"method": "workspace.events", "workspace": "Renamed"})["events"]))

    def test_rename_rejects_busy_or_conflicting_workspace(self):
        self.put(panes=[A])
        request = {"method": "workspace.rename", "workspace": "Test", "destination": "Renamed",
                   "revision": 1, "request_id": "rename-one"}
        token = "a" * 32
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})
        with self.assertRaisesRegex(Conflict, "attached"):
            self.store.dispatch(request)
        self.store.dispatch({"method": "workspace.lease.release", "workspace": "Test", "token": token})
        self.store.db.execute("INSERT INTO reservations VALUES (?,?)", (B, "Test"))
        with self.assertRaisesRegex(Conflict, "unfinished"):
            self.store.dispatch(request)
        self.store.db.execute("DELETE FROM reservations WHERE pane=?", (B,))
        self.store.db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", ("Renamed", 1, "other", "[]"))
        with self.assertRaisesRegex(Conflict, "already exists"):
            self.store.dispatch(request)
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [A])

    def test_rename_retired_destination_fences_stale_revisions(self):
        self.put(panes=[A])
        self.store.db.execute("INSERT INTO retired_workspaces VALUES (?,?)", ("Renamed", 99))
        result = self.store.dispatch({"method": "workspace.rename", "workspace": "Test", "destination": "Renamed",
                                      "revision": 1, "request_id": "rename-one"})
        self.assertGreater(result["revision"], 99)
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.store.observe_detached()
        self.assertFalse(self.store.dispatch({"method": "workspace.get", "workspace": "Renamed"})["exists"])
        previous = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "recreate",
                             "revision": previous["revision"], "snapshot": "# kzw-dormant-workspace\n", "panes": []})
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.store.observe_detached()
        self.assertTrue(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])

    def test_staging_preserves_authoritative_layout_and_rejects_stale_revision(self):
        self.put(panes=[A])
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, A + "\n", "")):
            self.store.dispatch({"method": "pane.reserve", "workspace": "Test", "pane": B,
                                 "request_id": "reserve", "revision": 1})
        request = {"method": "workspace.stage", "workspace": "Test", "revision": 1,
                   "snapshot": "staged layout", "panes": [A, B]}
        self.assertEqual(self.store.dispatch(request), self.store.dispatch(request))
        state = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual((state["revision"], state["snapshot"], state["panes"]), (1, "layout", [A]))
        self.assertEqual(state["staged"]["snapshot"], "staged layout")
        with self.assertRaises(Conflict):
            self.store.dispatch({**request, "revision": 0})
        self.put("publish", revision=1, panes=[A, B])
        state = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual(state["staged"]["snapshot"], "staged layout")

    def test_stage_recovery_requires_every_session_and_matching_reservations(self):
        self.put(panes=[A])
        missing_b = subprocess.CompletedProcess([], 0, A + "\n", "")
        with patch("kzw.remote.subprocess.run", return_value=missing_b):
            self.store.dispatch({"method": "pane.reserve", "workspace": "Test", "pane": B,
                                 "request_id": "reserve", "revision": 1})
        request = {"workspace": "Test", "revision": 1, "snapshot": "staged", "panes": [A, B]}
        self.store.dispatch({**request, "method": "workspace.stage"})
        recover = {**request, "method": "workspace.recover-stage", "request_id": "recovery"}
        with patch("kzw.remote.subprocess.run", return_value=missing_b):
            with self.assertRaises(Conflict):
                self.store.dispatch(recover)
        state = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual(state["panes"], [A])
        self.assertEqual(state["reservations"], [B])
        self.assertIsNotNone(state["staged"])
        self.assertEqual(self.store.dispatch(recover), self.store.dispatch(recover))
        state = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual(state["panes"], [A, B])
        self.assertEqual(state["reservations"], [])
        self.assertIsNone(state["staged"])

    def test_status_reports_only_owned_sessions_after_successful_listing(self):
        self.put()
        other = "kzw-" + "c" * 32
        listing = subprocess.CompletedProcess([], 0, A + "\n" + other + "\n", "")
        with patch("kzw.remote.subprocess.run", return_value=listing):
            self.assertEqual(self.store.dispatch({"method": "workspace.status", "workspace": "Test"}),
                             {"revision": 1, "live": [A], "missing": [B], "confirmed_missing": [B]})

    def test_status_listing_failure_does_not_report_absence(self):
        self.put()
        with patch("kzw.remote.subprocess.run", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.store.dispatch({"method": "workspace.status", "workspace": "Test"})
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [A, B])

    def test_detached_observer_records_confirmed_absence_and_retires(self):
        self.put(panes=[A])
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.store.observe_detached()
        state = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertFalse(state["exists"])
        events = self.store.dispatch({"method": "workspace.events", "workspace": "Test"})["events"]
        self.assertTrue(any(event["kind"] == "session.missing" and event["data"]["pane"] == A
                            for event in events))
        self.assertTrue(any(event["kind"] == "workspace.retired" for event in events))

    def test_recreate_uses_new_revision_and_dormant_is_not_retired(self):
        self.put(panes=[A])
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.store.observe_detached()
            retired = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
            self.assertFalse(retired["exists"])
            stale = {"method": "workspace.put", "workspace": "Test", "request_id": "stale",
                     "revision": 1, "snapshot": "old", "panes": []}
            with self.assertRaises(Conflict):
                self.store.dispatch(stale)
            new = {**stale, "request_id": "new", "revision": retired["revision"],
                   "snapshot": "# kzw-dormant-workspace\n"}
            self.store.dispatch(new)
            self.store.observe_detached()
            self.assertTrue(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])

    def test_active_lease_prevents_detached_retirement(self):
        self.put(panes=[A])
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": "a" * 32})
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.store.observe_detached()
        self.assertTrue(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])

    def test_workspace_kill_journals_only_owned_panes_and_fences_mutations(self):
        self.put(panes=[A])
        token = "a" * 32
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})
        request = {"method": "workspace.kill", "workspace": "Test", "revision": 1, "lease_token": token}
        self.assertEqual(self.store.dispatch(request)["pending_cleanup"], [A])
        self.assertEqual(self.store.dispatch({"method": "cleanup.list", "workspace": "Test"})["panes"], [A])
        with self.assertRaisesRegex(Conflict, "termination"):
            self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 2,
                                 "request_id": "late", "snapshot": "bad", "panes": [B], "lease_token": token})
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.store.dispatch({"method": "cleanup.retry", "workspace": "Test", "lease_token": token})
            self.store.dispatch({"method": "workspace.lease.release", "workspace": "Test", "token": token})
            self.store.observe_detached()
        self.assertFalse(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])

    def test_workspace_kill_rejects_stale_revision_and_pending_creation(self):
        self.put(panes=[A])
        token = "a" * 32
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})
        request = {"method": "workspace.kill", "workspace": "Test", "revision": 0, "lease_token": token}
        with self.assertRaisesRegex(Conflict, "changed"):
            self.store.dispatch(request)
        self.store.db.execute("INSERT INTO reservations VALUES (?,?)", (B, "Test"))
        with self.assertRaisesRegex(Conflict, "unfinished"):
            self.store.dispatch({**request, "revision": 1})
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [A])

    def test_kill_dormant_workspace_retires_it(self):
        self.store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "dormant",
                             "revision": 0, "snapshot": "# kzw-dormant-workspace\n", "panes": []})
        token = "a" * 32
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})
        self.store.dispatch({"method": "workspace.kill", "workspace": "Test", "revision": 1, "lease_token": token})
        self.store.dispatch({"method": "workspace.lease.release", "workspace": "Test", "token": token})
        self.store.observe_detached()
        self.assertFalse(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])

    def test_reservations_staging_and_cleanup_block_retirement(self):
        self.put(panes=[A])
        for table in ("reservations", "staged_layouts", "cleanup"):
            with self.subTest(table=table):
                self.store.db.execute("DELETE FROM reservations")
                self.store.db.execute("DELETE FROM staged_layouts")
                self.store.db.execute("DELETE FROM cleanup")
                if not self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"]:
                    self.fail("workspace was retired despite pending state")
                if table == "reservations":
                    self.store.db.execute("INSERT INTO reservations VALUES (?,?)", (B, "Test"))
                elif table == "staged_layouts":
                    self.store.db.execute("INSERT INTO staged_layouts VALUES (?,?,?,?)", ("Test", 1, "staged", "[]"))
                else:
                    self.store.db.execute("INSERT INTO cleanup VALUES (?,?)", ("Test", B))
                status = {"revision": 1, "live": [], "missing": [A], "confirmed_missing": [A]}
                self.assertFalse(self.store.retire_if_empty("Test", status))


    def test_detached_observer_treats_listing_failure_as_unknown(self):
        self.put(panes=[A])
        with patch("kzw.remote.subprocess.run", side_effect=OSError("unavailable")):
            self.store.observe_detached()
        events = self.store.dispatch({"method": "workspace.events", "workspace": "Test"})["events"]
        self.assertFalse(any(event["kind"] == "session.missing" for event in events))
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [A])

    def test_watch_reports_confirmed_status_change(self):
        self.put()
        previous = {"revision": 1, "live": [A, B], "missing": [], "confirmed_missing": []}
        listing = subprocess.CompletedProcess([], 0, A + "\n", "")
        initially_live = subprocess.CompletedProcess([], 0, A + "\n" + B + "\n", "")
        with patch("kzw.remote.subprocess.run", side_effect=[initially_live, listing]):
            self.assertEqual(self.store.dispatch({"method": "workspace.status", "workspace": "Test"}), previous)
            result = self.store.dispatch({"method": "workspace.watch", "workspace": "Test",
                                          "known": previous, "timeout": 0})
        self.assertEqual(result, {"revision": 1, "live": [A], "missing": [B], "confirmed_missing": [B]})

    def test_seen_status_survives_server_store_reopen(self):
        self.put()
        # Model a pane from an older database that has never been observed.
        self.store.db.execute("DELETE FROM observed_sessions WHERE pane=?", (B,))
        listing = subprocess.CompletedProcess([], 0, A + "\n", "")
        with patch("kzw.remote.subprocess.run", return_value=listing):
            self.store.dispatch({"method": "workspace.status", "workspace": "Test"})
        path = Path(self.temp.name) / "private" / "state.sqlite"
        self.store.close()
        self.store = Store(path)
        missing = subprocess.CompletedProcess([], 0, "", "")
        with patch("kzw.remote.subprocess.run", return_value=missing):
            status = self.store.dispatch({"method": "workspace.status", "workspace": "Test"})
        self.assertEqual(status["confirmed_missing"], [A])

    def test_watch_listing_failure_is_not_a_disappearance(self):
        self.put()
        with patch("kzw.remote.subprocess.run", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.store.dispatch({"method": "workspace.watch", "workspace": "Test", "timeout": 0})

    def test_events_are_durable_ordered_and_not_duplicated(self):
        self.put()
        initial = self.store.dispatch({"method": "workspace.sync", "workspace": "Test"})
        listing = subprocess.CompletedProcess([], 0, A + "\n", "")
        with patch("kzw.remote.subprocess.run", return_value=listing):
            self.store.dispatch({"method": "workspace.status", "workspace": "Test"})
            self.store.dispatch({"method": "workspace.status", "workspace": "Test"})
        events = self.store.dispatch({"method": "workspace.events", "workspace": "Test",
                                      "after": initial["event_cursor"]})["events"]
        self.assertEqual([(event["kind"], event["data"]) for event in events],
                         [("session.missing", {"pane": B})])
        path = Path(self.temp.name) / "private" / "state.sqlite"
        self.store.close()
        self.store = Store(path)
        self.assertEqual(self.store.dispatch({"method": "workspace.events", "workspace": "Test",
                                              "after": initial["event_cursor"]})["events"], events)
        self.assertEqual(self.store.dispatch({"method": "workspace.events", "workspace": "Other",
                                              "after": 0})["events"], [])

    def test_sync_cursor_and_event_wait_cover_subsequent_layout_change(self):
        self.put(panes=[A])
        snapshot = self.store.dispatch({"method": "workspace.sync", "workspace": "Test"})
        self.put("add", revision=1, panes=[A, B])
        result = self.store.dispatch({"method": "workspace.events.wait", "workspace": "Test",
                                      "after": snapshot["event_cursor"], "timeout": 0})
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual((event["kind"], event["revision"], event["data"]["reason"]),
                         ("layout.updated", 2, "snapshot"))
        self.assertEqual(event["data"]["panes"], [A, B])

    def test_event_wait_detects_absence_without_fabricating_exit(self):
        self.put(panes=[A])
        cursor = self.store.dispatch({"method": "workspace.sync", "workspace": "Test"})["event_cursor"]
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = self.store.dispatch({"method": "workspace.events.wait", "workspace": "Test",
                                          "after": cursor, "timeout": 1})
        self.assertEqual([(e["kind"], e["data"]) for e in result["events"]],
                         [("session.missing", {"pane": A})])

    def test_event_wait_listing_failure_does_not_record_absence(self):
        self.put(panes=[A])
        cursor = self.store.dispatch({"method": "workspace.sync", "workspace": "Test"})["event_cursor"]
        with patch("kzw.remote.subprocess.run", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                self.store.dispatch({"method": "workspace.events.wait", "workspace": "Test",
                                     "after": cursor, "timeout": 1})
        self.assertEqual(self.store.dispatch({"method": "workspace.events", "workspace": "Test",
                                              "after": cursor})["events"], [])

    def test_ordinary_snapshots_cannot_remove_panes(self):
        self.put()
        with self.assertRaises(ValueError):
            self.put("two", revision=1, panes=[A])

    def test_registration_requires_every_new_session_to_exist(self):
        incomplete = subprocess.CompletedProcess([], 0, A + "\n", "")
        with patch("kzw.remote.subprocess.run", return_value=incomplete):
            with self.assertRaises(Conflict):
                self.put()
        self.assertFalse(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])

    def test_not_ready_response_explicitly_identifies_rolled_back_registration(self):
        request = {"method": "workspace.put", "workspace": "Test", "request_id": "test", "revision": 0,
                   "snapshot": "layout", "panes": [A, B]}
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, A + "\n", "")):
            result = response(self.store, json.dumps(request).encode())
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "session_not_ready")
        self.assertFalse(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])

    def test_registration_listing_failure_preserves_state(self):
        with patch("kzw.remote.subprocess.run", side_effect=OSError("listing unavailable")):
            with self.assertRaises(OSError):
                self.put()
        self.assertFalse(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])

    def test_reservation_survives_retry_and_is_consumed_by_registration(self):
        self.put()
        pane = "kzw-" + "c" * 32
        request = {"method": "pane.reserve", "workspace": "Test", "pane": pane,
                   "request_id": "reserve-one", "revision": 1}
        self.assertEqual(self.store.dispatch(request), self.store.dispatch(request))
        self.assertEqual(self.store.dispatch(request), self.store.dispatch({**request, "revision": 2}))
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["reservations"], [pane])
        listing = subprocess.CompletedProcess([], 0, A + "\n" + B + "\n" + pane + "\n", "")
        with patch("kzw.remote.subprocess.run", return_value=listing):
            with self.assertRaises(Conflict):
                self.store.dispatch({"method": "workspace.put", "workspace": "other", "request_id": "steal",
                                     "revision": 0, "snapshot": "layout", "panes": [pane]})
            self.put("register", revision=1, panes=[A, B, pane])
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["reservations"], [])

    def test_reservation_rejects_stale_workspace_revision(self):
        self.put()
        with self.assertRaises(Conflict):
            self.store.dispatch({"method": "pane.reserve", "workspace": "Test", "pane": "kzw-" + "c" * 32,
                                 "request_id": "reserve", "revision": 0})

    def test_explicit_close_records_intent_before_cleanup(self):
        self.put()
        req = {"method": "panes.close", "workspace": "Test", "request_id": "close-B", "revision": 1,
               "snapshot": "new layout", "panes": [A], "remove": [B]}
        self.assertEqual(self.store.dispatch(req)["pending_cleanup"], [B])
        self.assertEqual(self.store.dispatch(req)["revision"], 2)
        self.assertEqual(self.store.dispatch({"method": "cleanup.list", "workspace": "Test"})["panes"], [B])
        events = self.store.dispatch({"method": "workspace.events", "workspace": "Test", "after": 0})["events"]
        layout = events[-1]
        self.assertEqual((layout["data"]["reason"], layout["data"]["pending_cleanup"]), ("managed_close", [B]))
        cursor = layout["seq"]
        listing = subprocess.CompletedProcess([], 0, B + "\n", "")
        failed = subprocess.CompletedProcess([], 1, "", "not available")
        with patch("kzw.remote.subprocess.run", side_effect=[listing, failed]):
            self.assertEqual(self.store.dispatch({"method": "cleanup.retry", "workspace": "Test"})["remaining"], [B])
        self.assertEqual(self.store.dispatch({"method": "workspace.events", "workspace": "Test", "after": cursor})["events"], [])
        with patch("kzw.remote.subprocess.run", side_effect=[listing, subprocess.CompletedProcess([], 0, "", ""),
                                                            subprocess.CompletedProcess([], 0, "", "")]) as run:
            self.assertEqual(self.store.dispatch({"method": "cleanup.retry", "workspace": "Test"})["remaining"], [])
            self.assertEqual(run.call_args_list[1].args[0], ["zmx", "kill", B])
        self.assertEqual(self.store.dispatch({"method": "cleanup.list", "workspace": "Test"})["panes"], [])
        events = self.store.dispatch({"method": "workspace.events", "workspace": "Test", "after": cursor})["events"]
        self.assertEqual([(e["kind"], e["data"]) for e in events],
                         [("pane.cleanup_completed", {"pane": B, "reason": "managed_close"})])

    def test_pending_panes_cannot_be_readopted(self):
        self.put()
        self.store.dispatch({"method": "panes.close", "workspace": "Test", "request_id": "close", "revision": 1,
                             "snapshot": "new", "panes": [A], "remove": [B]})
        with self.assertRaises(Conflict):
            self.put("three", revision=2)

    def test_pane_cannot_belong_to_two_workspaces(self):
        self.put()
        with self.assertRaises(Conflict):
            self.store.dispatch({"method": "workspace.put", "workspace": "other", "request_id": "one",
                                 "revision": 0, "snapshot": "other", "panes": [B]})

    def test_close_rejects_unowned_pane(self):
        self.put()
        with self.assertRaises(ValueError):
            self.store.dispatch({"method": "panes.close", "workspace": "Test", "request_id": "close", "revision": 1,
                                 "snapshot": "new", "panes": [A], "remove": ["kzw-" + "c" * 32]})

    def test_lease_fences_old_and_unidentified_writers(self):
        self.put()
        token1, token2 = "a" * 32, "b" * 32
        acquire = {"method": "workspace.lease.acquire", "workspace": "Test", "token": token1}
        with patch("kzw.remote.time.time", return_value=100):
            self.assertTrue(self.store.dispatch(acquire)["held"])
            self.assertTrue(self.store.dispatch(acquire)["held"])
            with self.assertRaises(Conflict):
                self.store.dispatch({**acquire, "token": token2})
            with self.assertRaises(Conflict):
                self.put("unidentified", revision=1)
            self.store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "owner",
                                 "revision": 1, "snapshot": "new", "panes": [A, B], "lease_token": token1})
        with patch("kzw.remote.time.time", return_value=131):
            with self.assertRaises(Conflict):
                self.store.dispatch({"method": "workspace.lease.renew", "workspace": "Test", "token": token1})
            self.assertTrue(self.store.dispatch({**acquire, "token": token2})["held"])
            with self.assertRaises(Conflict):
                self.store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "stale",
                                     "revision": 2, "snapshot": "stale", "panes": [A, B], "lease_token": token1})
            with self.assertRaises(Conflict):
                self.store.dispatch({"method": "cleanup.retry", "workspace": "Test", "lease_token": token1})

    def test_released_token_cannot_be_reused(self):
        self.put()
        token = "a" * 32
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})
        self.store.dispatch({"method": "workspace.lease.release", "workspace": "Test", "token": token})
        with self.assertRaises(Conflict):
            self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})
        with self.assertRaises(Conflict):
            self.put("unsafe", revision=1)


class RemoteServerTests(unittest.TestCase):
    def test_server_observes_absence_without_connected_clients(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ks-", dir=cache) as directory:
            root = Path(directory) / "kzw"
            root.mkdir(mode=0o700)
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            zmx = fake_bin / "zmx"
            zmx.write_text("#!/bin/sh\nexit 0\n")
            zmx.chmod(0o700)
            store = Store(root / "remote.sqlite3")
            store.db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", ("Test", 1, "layout", json.dumps([A])))
            store.db.execute("INSERT INTO observed_sessions VALUES (?,?)", ("Test", A))
            store.db.execute("INSERT INTO session_presence VALUES (?,?,?)", ("Test", A, 1))
            store.close()
            env = os.environ.copy()
            env.update(XDG_STATE_HOME=directory, PATH=str(fake_bin) + os.pathsep + env.get("PATH", ""), HOME=directory)
            env.pop("KZW_LOCAL_COORDINATOR", None)
            server = subprocess.Popen([sys.executable, "-m", "kzw.remote", "--serve"], env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 8
                observed = False
                while time.monotonic() < deadline and server.poll() is None:
                    store = Store(root / "remote.sqlite3")
                    try:
                        events = store.dispatch({"method": "workspace.events", "workspace": "Test"})["events"]
                        observed = any(event["kind"] == "session.missing" for event in events)
                    finally:
                        store.close()
                    if observed:
                        break
                    time.sleep(0.05)
                self.assertTrue(observed, "server did not observe missing session without a client")
            finally:
                if server.poll() is None:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(3)
                        client.connect(str(root / "server.sock"))
                        client.sendall(b'{"method":"server.shutdown"}\n')
                        client.recv(4096)
                    server.wait(timeout=5)

    def test_bridge_rejects_server_with_different_build_but_allows_stop(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ks-", dir=cache) as directory:
            env = os.environ.copy()
            env.update({"XDG_STATE_HOME": directory, "KZW_COORDINATOR_SOURCE": "old-build"})
            server = subprocess.Popen([sys.executable, "-m", "kzw.remote", "--serve"], env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                path = Path(directory) / "kzw" / "server.sock"
                deadline = time.monotonic() + 5
                while not path.exists() and time.monotonic() < deadline and server.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(path.exists())
                env["KZW_COORDINATOR_SOURCE"] = "new-build"
                def bridge(req):
                    result = subprocess.run([sys.executable, "-m", "kzw.remote"], env=env,
                                            input=json.dumps(req) + "\n", text=True, capture_output=True,
                                            check=True, timeout=5)
                    return json.loads(result.stdout)
                self.assertFalse(bridge({"method": "workspace.list"})["ok"])
                self.assertTrue(bridge({"method": "server.shutdown"})["result"]["stopping"])
                server.wait(timeout=5)
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=5)

    def test_server_does_not_replace_non_socket_file(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ks-", dir=cache) as directory:
            root = Path(directory) / "kzw"
            root.mkdir(mode=0o700)
            path = root / "server.sock"
            path.write_text("do not replace")
            env = os.environ.copy()
            env["XDG_STATE_HOME"] = directory
            result = subprocess.run([sys.executable, "-m", "kzw.remote", "--serve"], env=env,
                                    capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(), "do not replace")

    def test_bridge_starts_server_and_reconnects_after_bridge_exits(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ks-", dir=cache) as directory:
            env = os.environ.copy()
            env["XDG_STATE_HOME"] = directory
            def call(req):
                proc = subprocess.run([sys.executable, "-m", "kzw.remote"], env=env,
                                      input=json.dumps(req, separators=(",", ":")) + "\n",
                                      capture_output=True, text=True, check=True, timeout=10)
                return json.loads(proc.stdout)
            try:
                request = {"method": "workspace.put", "workspace": "Test", "request_id": "create",
                           "revision": 0, "snapshot": "layout", "panes": []}
                self.assertEqual(call(request)["result"]["revision"], 1)
                self.assertEqual(call({"method": "workspace.get", "workspace": "Test"})["result"]["panes"], [])
            finally:
                call({"method": "server.shutdown"})
                path = Path(directory) / "kzw" / "server.sock"
                deadline = time.monotonic() + 5
                while path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(path.exists(), "test coordinator failed to stop")

    def test_server_refuses_shutdown_with_another_connected_client(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ks-", dir=cache) as directory:
            env = os.environ.copy()
            env["XDG_STATE_HOME"] = directory
            server = subprocess.Popen([sys.executable, "-m", "kzw.remote", "--serve"], env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                path = Path(directory) / "kzw" / "server.sock"
                deadline = time.monotonic() + 5
                while not path.exists() and time.monotonic() < deadline and server.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(path.exists())
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first, \
                     socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second:
                    first.settimeout(3)
                    second.settimeout(3)
                    first.connect(str(path))
                    first.sendall(b'{"method":"server.info"}\n')
                    self.assertTrue(json.loads(first.makefile("rb").readline())["ok"])
                    second.connect(str(path))
                    second.sendall(b'{"method":"server.shutdown"}\n')
                    self.assertFalse(json.loads(second.makefile("rb").readline())["ok"])
                self.assertIsNone(server.poll())
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=5)

    def test_server_retains_state_across_independent_connections(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ks-", dir=cache) as directory:
            env = os.environ.copy()
            env["XDG_STATE_HOME"] = directory
            server = subprocess.Popen([sys.executable, "-m", "kzw.remote", "--serve"], env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                path = Path(directory) / "kzw" / "server.sock"
                deadline = time.monotonic() + 5
                while not path.exists() and time.monotonic() < deadline and server.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(path.exists(), "test coordinator did not start")

                def call(request):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(3)
                        client.connect(str(path))
                        client.sendall(json.dumps(request).encode() + b"\n")
                        with client.makefile("rb") as reader:
                            return json.loads(reader.readline())

                created = call({"method": "workspace.put", "workspace": "Test", "request_id": "create",
                                "revision": 0, "snapshot": "layout", "panes": []})
                self.assertEqual(created["result"]["revision"], 1)
                read = call({"method": "workspace.get", "workspace": "Test"})
                self.assertEqual(read["result"]["panes"], [])
                self.assertIsNone(server.poll())
                # A second daemon for the same state directory must not
                # replace the active listener or steal its lock.
                duplicate = subprocess.run([sys.executable, "-m", "kzw.remote", "--serve"], env=env,
                                           capture_output=True, timeout=5)
                self.assertEqual(duplicate.returncode, 0)
                self.assertEqual(call({"method": "workspace.get", "workspace": "Test"})["result"]["panes"], [])
                server.terminate()
                server.wait(timeout=5)
                # After an abrupt stop, a new owner removes only its own stale
                # socket and opens the existing SQLite state.
                server = subprocess.Popen([sys.executable, "-m", "kzw.remote", "--serve"], env=env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        restored = call({"method": "workspace.get", "workspace": "Test"})
                        break
                    except (ConnectionRefusedError, FileNotFoundError):
                        time.sleep(0.02)
                else:
                    self.fail("server did not recover after interruption")
                self.assertEqual(restored["result"]["panes"], [])
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
