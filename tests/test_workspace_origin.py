import json
import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kzw import cli


class WorkspaceOriginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_same_name_is_separate_for_local_and_remote_hosts(self):
        local = cli.state_dir("Test")
        (local / "workspace.kitty-session").write_text("local state")
        first = cli.state_dir("Test", "origin")
        second = cli.state_dir("Test", "other")
        self.assertEqual(len({local, first, second}), 3)
        self.assertEqual((local / "workspace.kitty-session").read_text(), "local state")
        self.assertEqual(first, cli.state_dir("Test", "origin"))

    def test_legacy_remote_cache_remains_in_place(self):
        legacy = cli.state_dir("Test")
        (legacy / "remote.json").write_text(json.dumps({"host": "origin", "workspace": "Test", "pending": {}}))
        self.assertEqual(cli.state_dir("Test", "origin"), legacy)
        self.assertNotEqual(cli.state_dir("Test", "other"), legacy)
        self.assertIn("pending", json.loads((legacy / "remote.json").read_text()))

    def test_ambiguous_legacy_and_scoped_cache_fails_closed(self):
        scoped = cli.state_dir("Test", "origin")
        legacy = cli.state_dir("Test")
        (legacy / "remote.json").write_text(json.dumps({"host": "origin", "workspace": "Test"}))
        with self.assertRaisesRegex(ValueError, "both legacy"):
            cli.state_dir("Test", "origin")
        self.assertTrue(scoped.exists())

    def test_invalid_host_cannot_escape_namespace(self):
        with self.assertRaises(ValueError):
            cli.state_dir("Test", "../other")

    def test_remote_create_does_not_touch_local_workspace(self):
        local = cli.state_dir("Test")
        snapshot = local / "workspace.kitty-session"
        snapshot.write_text("local state")
        with patch("kzw.cli.remote_client.create") as create:
            cli.create_remote("Test", "origin")
        self.assertNotEqual(create.call_args.args[0], local)
        self.assertEqual(snapshot.read_text(), "local state")

    def test_detach_targets_requested_origin(self):
        remote = cli.state_dir("Test", "origin")
        with patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked", return_value=1) as capture, \
             patch("kzw.cli.query") as query:
            cli.detach("Test", "origin")
        self.assertEqual(capture.call_args.args[0], remote)
        self.assertEqual(query.call_args.args[0], remote / "kitty.sock")

    def test_attach_holds_local_lock_through_run_and_releases_lease(self):
        directory = cli.state_dir("Test", "origin")
        def running(*args, **kwargs):
            self.assertIsNotNone(kwargs["existing_lock"])
            with open(directory / "lock", "a+") as competing:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return 0
        with patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.remote_client.acquire_lease") as acquire, \
             patch("kzw.cli.remote_client.start_lease_heartbeat", return_value=lambda: None), \
             patch("kzw.cli.remote_client.pull"), \
             patch("kzw.cli.remote_client.release_lease") as release, \
             patch("kzw.cli.run", side_effect=running):
            self.assertEqual(cli.attach_remote("Test", "origin"), 0)
        acquire.assert_called_once_with(directory, "Test", "origin")
        release.assert_called_once_with(directory)
