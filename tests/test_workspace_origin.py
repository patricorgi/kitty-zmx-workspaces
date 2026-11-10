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

    def test_local_coordinator_create_refuses_to_overwrite_legacy_workspace(self):
        from kzw import remote_client
        local = cli.state_dir("Test")
        snapshot = local / "workspace.kitty-session"
        snapshot.write_text("legacy layout\n")
        with patch("kzw.remote_client.call") as call:
            with self.assertRaisesRegex(ValueError, "already exists"):
                remote_client.create(local, "Test", "")
        call.assert_not_called()
        self.assertEqual(snapshot.read_text(), "legacy layout\n")

    def test_create_accepts_only_empty_legacy_remnants(self):
        from kzw import remote_client
        local = cli.state_dir("Test")
        for name in ("lock", "capture.lock", "watcher.changed"):
            (local / name).touch()
        (local / "native-pending").mkdir()
        with patch("kzw.remote_client.call", return_value={"exists": False}), \
             patch("kzw.remote_client.publish"):
            remote_client.create(local, "Test", "")
        self.assertTrue((local / "remote.json").is_file())
        self.assertTrue((local / "native-pending").is_dir())
        self.assertEqual((local / "capture.lock").stat().st_size, 0)

    def test_create_rejects_nonempty_unknown_and_symlink_remnants(self):
        from kzw import remote_client
        for kind in ("nonempty-file", "pending", "unknown", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                if kind == "nonempty-file":
                    (directory / "watcher.changed").write_text("data")
                elif kind == "pending":
                    (directory / "native-pending").mkdir()
                    (directory / "native-pending" / "operation").touch()
                elif kind == "unknown":
                    (directory / "unknown").touch()
                else:
                    (directory / "capture.lock").symlink_to(directory / "missing")
                with patch("kzw.remote_client.call") as call:
                    with self.assertRaisesRegex(ValueError, "already exists"):
                        remote_client.create(directory, "Test", "")
                    call.assert_not_called()

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
