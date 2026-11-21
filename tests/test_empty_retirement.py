import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kzw import cli


class EmptyRetirementTests(unittest.TestCase):
    def test_final_close_retries_retirement_without_repeating_session_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            pending = directory / "pending-close.json"
            pane = "kzw-" + "a" * 32
            pending.write_text(json.dumps({"phase": "layout-saved", "last": True, "remote": True,
                                           "panes": [{"pane": pane, "host": "origin"}]}))
            (directory / "workspace.kitty-session").write_text(cli.EMPTY_SNAPSHOT)
            (directory / "remote.json").write_text(json.dumps({
                "host": "origin", "workspace": "Test", "revision": 4, "panes": [],
            }))
            empty = {"exists": True, "panes": [], "revision": 4, "snapshot": cli.EMPTY_SNAPSHOT}
            with patch.object(cli.remote_client, "retry_cleanup") as cleanup, \
                 patch.object(cli.remote_client, "origin_state_home", return_value=""), \
                 patch.object(cli.remote_client, "lease_token", return_value="lease"), \
                 patch.object(cli.remote_client, "call", side_effect=[empty, OSError("lost reply"),
                                                                       {"exists": False, "revision": 6}]) as call:
                with self.assertRaisesRegex(OSError, "lost reply"):
                    cli.cleanup_close_locked(directory)
                self.assertEqual(json.loads(pending.read_text())["retire_revision"], 4)
                self.assertEqual(cli.cleanup_close_locked(directory), [pane])
                cleanup.assert_called_once_with(directory)
                self.assertEqual(call.call_args_list[1].args[1], {
                    "method": "workspace.kill", "workspace": "Test", "revision": 4, "lease_token": "lease",
                })
            self.assertFalse(pending.exists())

    def test_only_exact_empty_unattached_workspace_is_retired(self):
        for reason in ("empty", "dormant", "live", "local", "journal", "view", "stale", "reserved"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                snapshot = "# kzw-dormant-workspace\n" if reason == "dormant" else cli.EMPTY_SNAPSHOT
                (directory / "workspace.kitty-session").write_text(snapshot)
                (directory / "remote.json").write_text(json.dumps({
                    "workspace": "Test", "host": "origin", "revision": 4, "panes": [],
                }))
                if reason == "journal":
                    (directory / "pending-exit.json").touch()
                state = {"exists": True, "snapshot": snapshot, "revision": 5 if reason == "stale" else 4,
                         "panes": ["kzw-" + "a" * 32] if reason == "live" else [],
                         "reservations": ["kzw-" + "b" * 32] if reason == "reserved" else []}
                with patch("kzw.cli.view_state", return_value={} if reason == "view" else None), \
                     patch("kzw.local_tabs.request", return_value={"tab": {} if reason == "local" else None}), \
                     patch("kzw.cli.remote_client.origin_state_home", return_value=""), \
                     patch("kzw.cli.remote_client.lease_token", return_value="lease"), \
                     patch("kzw.cli.remote_client.call", return_value=state) as call:
                    self.assertEqual(cli.retire_empty_coordinator_workspace(directory, "Test", "origin"),
                                     reason == "empty")
                kills = [args.args[1] for args in call.call_args_list if args.args[1]["method"] == "workspace.kill"]
                self.assertEqual(kills, [{"method": "workspace.kill", "workspace": "Test", "revision": 4,
                                          "lease_token": "lease"}] if reason == "empty" else [])


if __name__ == "__main__":
    unittest.main()
