import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kzw import cli


class EmptyRetirementTests(unittest.TestCase):
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
