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

    def test_create_attach_routes_local_and_remote(self):
        for host in ("", "origin"):
            with self.subTest(host=host):
                argv = ["kzw", "create", "Test", "--attach", "--hold"] + (["--host", host] if host else [])
                with patch("sys.argv", argv), patch("kzw.cli.create_remote") as create, \
                     patch("kzw.cli.attach_remote", return_value=7) as attach:
                    self.assertEqual(cli.main(), 7)
                create.assert_called_once_with("Test", host)
                attach.assert_called_once_with("Test", host)

    def test_create_does_not_attach_on_failure_or_without_flag(self):
        with patch("sys.argv", ["kzw", "create", "Test", "--attach", "--hold"]), \
             patch("kzw.cli.create_remote", side_effect=ValueError("conflict")), \
             patch("kzw.cli.attach_remote") as attach:
            self.assertEqual(cli.main(), 1)
            attach.assert_not_called()
        with patch("sys.argv", ["kzw", "create", "Test"]), \
             patch("kzw.cli.create_remote"), patch("kzw.cli.attach_remote") as attach:
            self.assertEqual(cli.main(), 0)
            attach.assert_not_called()

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

    def test_workspace_kill_cancel_does_not_acquire_lease_or_mutate(self):
        state = {"exists": True, "revision": 3, "panes": ["kzw-" + "a" * 32]}
        with patch("kzw.cli.remote_client.call", return_value=state) as call, \
             patch("kzw.cli.remote_client.acquire_lease") as acquire, \
             patch("builtins.input", return_value="n"):
            cli.kill_workspace("Test", "")
        call.assert_called_once_with("", {"method": "workspace.get", "workspace": "Test"})
        acquire.assert_not_called()

    def test_rename_recovers_lost_acknowledgement(self):
        from kzw.remote import Store
        store = Store(Path(self.temp.name) / "server.sqlite")
        self.addCleanup(store.close)
        store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "create",
                        "revision": 0, "snapshot": "# kzw-dormant-workspace\n", "panes": []})
        dropped = [False]
        def call(host, request):
            result = store.dispatch(request)
            if request["method"] == "workspace.rename" and not dropped[0]:
                dropped[0] = True
                raise OSError("connection lost after commit")
            return result
        with patch("kzw.cli.remote_client.call", side_effect=call):
            with self.assertRaises(OSError):
                cli.rename_workspace("Test", "Renamed", "origin")
            cli.rename_workspace("Test", "Renamed", "origin")
        self.assertFalse(store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])
        self.assertTrue(store.dispatch({"method": "workspace.get", "workspace": "Renamed"})["exists"])
        self.assertFalse(list((Path(self.temp.name) / "kzw/rename-intents").glob("*.json")))

    def test_rename_refuses_existing_destination_cache(self):
        target = cli.state_dir("Renamed", "origin")
        (target / "workspace.kitty-session").write_text("preserve")
        with patch("kzw.cli.remote_client.call") as call:
            with self.assertRaisesRegex(ValueError, "destination"):
                cli.rename_workspace("Test", "Renamed", "origin")
        call.assert_not_called()
        self.assertEqual((target / "workspace.kitty-session").read_text(), "preserve")

    def test_rename_retries_after_destination_cache_was_written(self):
        from kzw.remote import Store
        store = Store(Path(self.temp.name) / "server.sqlite")
        self.addCleanup(store.close)
        store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "create",
                        "revision": 0, "snapshot": "# kzw-dormant-workspace\n", "panes": []})
        original = Path.unlink
        dropped = [False]
        def unlink(path, *args, **kwargs):
            if path.parent.name == "rename-intents" and path.suffix == ".json" and not dropped[0]:
                dropped[0] = True
                raise OSError("simulated interruption")
            return original(path, *args, **kwargs)
        with patch("kzw.cli.remote_client.call", side_effect=lambda host, request: store.dispatch(request)), \
             patch.object(Path, "unlink", autospec=True, side_effect=unlink):
            with self.assertRaises(OSError):
                cli.rename_workspace("Test", "Renamed", "origin")
            cli.rename_workspace("Test", "Renamed", "origin")
        target = cli.state_dir("Renamed", "origin")
        self.assertEqual(json.loads((target / "remote.json").read_text())["workspace"], "Renamed")

    def test_host_origin_rename_replays_after_source_disappears(self):
        from kzw.remote import Store
        root = Path(self.temp.name) / "kzw"
        root.mkdir()
        (root / "server.sock").touch()
        store = Store(Path(self.temp.name) / "server.sqlite")
        self.addCleanup(store.close)
        store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "create",
                        "revision": 0, "snapshot": "# kzw-dormant-workspace\n", "panes": []})
        dropped = [False]
        def call(request):
            result = store.dispatch(request)
            if request["method"] == "workspace.rename" and not dropped[0]:
                dropped[0] = True
                raise OSError("lost acknowledgement")
            return result
        with patch("kzw.sessions.coordinator_request", side_effect=call):
            with self.assertRaises(OSError):
                cli.rename_workspace("Test", "Renamed")
            cli.rename_workspace("Test", "Renamed")
        self.assertTrue(store.dispatch({"method": "workspace.get", "workspace": "Renamed"})["exists"])

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

    def test_create_accepts_background_logs_for_local_and_remote_origins(self):
        from kzw import remote_client
        for host in ("", "origin"):
            for text in ("", "previous startup failed\n"):
                with self.subTest(host=host, text=text), tempfile.TemporaryDirectory() as temp:
                    directory = Path(temp)
                    (directory / "lock").touch()
                    log = directory / "supervisor.log"
                    log.write_text(text)
                    with patch("kzw.remote_client.call", return_value={"exists": False}) as call, \
                         patch("kzw.remote_client.publish") as publish:
                        remote_client.create(directory, "Test", host)
                    call.assert_called_once_with(host, {"method": "workspace.get", "workspace": "Test"})
                    publish.assert_called_once_with(directory, remote_client.DORMANT, [])
                    self.assertEqual(log.read_text(), text)

    def test_create_and_first_pull_reject_unsafe_logs_and_real_workspace_state(self):
        from kzw import remote_client
        for method in ("create", "pull"):
            for kind in ("log-directory", "log-symlink", "unknown", "layout", "pending-exit"):
                with self.subTest(method=method, kind=kind), tempfile.TemporaryDirectory() as temp:
                    directory = Path(temp)
                    log = directory / "supervisor.log"
                    if kind == "log-directory":
                        log.mkdir()
                    elif kind == "log-symlink":
                        log.symlink_to(directory / "missing")
                    else:
                        log.write_text("diagnostic\n")
                        name = {"unknown": "unknown", "layout": "workspace.kitty-session",
                                "pending-exit": "pending-exit.json"}[kind]
                        (directory / name).write_text("preserve\n")
                    with patch("kzw.remote_client.call") as call, self.assertRaisesRegex(ValueError, "already exists"):
                        if method == "create":
                            remote_client.create(directory, "Test", "origin")
                        else:
                            remote_client.pull(directory, "Test", "origin", "/bin/kzw-pane")
                    call.assert_not_called()
                    if kind not in ("log-directory", "log-symlink"):
                        self.assertEqual((directory / name).read_text(), "preserve\n")

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
        pane = "kzw-" + "a" * 32
        (remote / "panes.json").write_text(json.dumps([pane]))
        tree = [{"tabs": [{"windows": [{"id": 7, "user_vars": {"kzw_pane": pane, "kzw_host": "origin"}}]}]}]
        with patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked", return_value=1) as capture, \
             patch("kzw.cli.query", side_effect=[json.dumps(tree), ""]) as query:
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
