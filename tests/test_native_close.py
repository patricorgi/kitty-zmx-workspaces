import json
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kzw import cli, native_close
from kzw.core import FLAG


A, B = ("kzw-" + c * 32 for c in "ab")


def snapshot(panes):
    return "".join(shlex.join(["launch", FLAG + json.dumps({"id": wid}), "/bin/kzw-pane",
                               "--pane", pane, "--host", "", "--existing"]) + "\n"
                   for wid, pane in panes.items())


class NativeCloseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name) / "Test"
        cli.write_view(self.directory, "unix:/test", os.getpid(), 7, [A, B])
        self.path = self.directory / native_close.RECEIPTS
        self.pending = self.directory / "pending-close.json"
        self.saved = self.directory / "workspace.kitty-session"
        self.saved.write_text(snapshot({3: A, 4: B}))
        (self.directory / "panes.json").write_text(json.dumps([A, B]))

    def notify(self, data, pane=A, wid=3, os_window_id=7):
        window = SimpleNamespace(id=wid, os_window_id=os_window_id,
                                 user_vars={"kzw_directory": str(self.directory), "kzw_workspace": "Test",
                                            "kzw_host": "", "kzw_pane": pane})
        native_close.on_close(None, window, data)

    def confirmed(self):
        self.notify({"reason": "window", "cause": "closed", "confirmed": True})

    def test_only_confirmed_individual_close_is_recorded(self):
        for reason, cause, confirmed in (("window", "closed", False), ("window", "program_exit", True),
                                         ("os_window", "closed", True), ("quit", "action", True)):
            self.notify(dict(reason=reason, cause=cause, confirmed=confirmed))
        self.notify(dict(reason="window", cause="closed", confirmed=True), os_window_id=9)
        self.assertFalse(self.path.exists())
        self.confirmed()
        generation = cli.view_state(self.directory)["generation"]
        self.assertEqual(json.loads(self.path.read_text()), [generation, 3, A, ""])

    def test_unexplained_missing_pane_and_stale_receipt_do_not_authorize_cleanup(self):
        self.confirmed()
        with patch.object(cli, "list_os_windows", return_value=[]), patch.object(cli, "cleanup_close_locked") as cleanup:
            native_close.synchronize(self.directory, "/bin/kzw-pane")
            cli.write_view(self.directory, "unix:/test", os.getpid(), 7, [A, B])
            native_close.synchronize(self.directory, "/bin/kzw-pane")
            cleanup.assert_not_called()
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.saved.read_text(), snapshot({3: A, 4: B}))

    def test_cleanup_failure_retains_exact_plan_after_publication(self):
        self.confirmed()
        remaining = [{"id": 7, "tabs": [{"windows": [{"id": 4, "user_vars": {"kzw_pane": B}}]}]}]
        with patch.object(cli, "list_os_windows", return_value=remaining), \
             patch("kzw.exits.snapshot_for_exit", return_value=snapshot({4: B})), \
             patch.object(cli, "cleanup_close_locked", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(OSError, "unavailable"):
                native_close.synchronize(self.directory, "/bin/kzw-pane")
        self.assertEqual(self.saved.read_text(), snapshot({4: B}))
        journal = json.loads(self.pending.read_text())
        self.assertEqual(journal["phase"], "layout-saved")
        self.assertEqual(journal["panes"], [{"pane": A, "host": ""}])

    def test_unreachable_kitty_does_not_publish_even_with_last_pane_receipt(self):
        self.saved.write_text(snapshot({3: A}))
        (self.directory / "panes.json").write_text(json.dumps([A]))
        self.confirmed()
        with patch.object(cli, "list_os_windows", side_effect=OSError("socket closed")), \
             patch.object(cli, "cleanup_close_locked") as cleanup:
            with self.assertRaisesRegex(OSError, "socket closed"):
                native_close.synchronize(self.directory, "/bin/kzw-pane")
            cleanup.assert_not_called()
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.saved.read_text(), snapshot({3: A}))
