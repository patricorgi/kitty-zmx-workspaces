import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kzw.remote import Conflict, Store

A = "kzw-" + "a" * 32
B = "kzw-" + "b" * 32


class RemoteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "private" / "state.sqlite")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def put(self, request_id="one", revision=0, panes=None):
        return self.store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": request_id,
                                    "revision": revision, "snapshot": "layout", "panes": panes or [A, B]})

    def test_compare_and_swap_and_idempotency(self):
        self.assertEqual(self.put(), self.put())
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["revision"], 1)
        with self.assertRaises(Conflict):
            self.put("one", revision=1)
        with self.assertRaises(Conflict):
            self.put("two", revision=0)

    def test_ordinary_snapshots_cannot_remove_panes(self):
        self.put()
        with self.assertRaises(ValueError):
            self.put("two", revision=1, panes=[A])

    def test_explicit_close_records_intent_before_cleanup(self):
        self.put()
        req = {"method": "panes.close", "workspace": "Test", "request_id": "close-B", "revision": 1,
               "snapshot": "new layout", "panes": [A], "remove": [B]}
        self.assertEqual(self.store.dispatch(req)["pending_cleanup"], [B])
        self.assertEqual(self.store.dispatch(req)["revision"], 2)
        self.assertEqual(self.store.dispatch({"method": "cleanup.list", "workspace": "Test"})["panes"], [B])
        listing = subprocess.CompletedProcess([], 0, B + "\n", "")
        failed = subprocess.CompletedProcess([], 1, "", "not available")
        with patch("kzw.remote.subprocess.run", side_effect=[listing, failed]):
            self.assertEqual(self.store.dispatch({"method": "cleanup.retry", "workspace": "Test"})["remaining"], [B])
        with patch("kzw.remote.subprocess.run", side_effect=[listing, subprocess.CompletedProcess([], 0, "", "")]) as run:
            self.assertEqual(self.store.dispatch({"method": "cleanup.retry", "workspace": "Test"})["remaining"], [])
            self.assertEqual(run.call_args_list[1].args[0], ["zmx", "kill", B])
        self.assertEqual(self.store.dispatch({"method": "cleanup.list", "workspace": "Test"})["panes"], [])

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


if __name__ == "__main__":
    unittest.main()
