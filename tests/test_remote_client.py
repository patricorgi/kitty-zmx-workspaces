import json
import tempfile
import unittest
import subprocess
import sys
import threading
import types
from pathlib import Path
from unittest.mock import patch

from kzw import remote_client
from kzw.remote import Store

PANE = "kzw-" + "a" * 32
SNAPSHOT = ("# kzw-focus {\"pane\":\"" + PANE + "\"}\n"
            "new_tab Test\nlayout splits\ncd /old/computer\n"
            "launch 'kitty-unserialize-data={\"id\":1}' --cwd=/old/computer "
            "--var=kzw_host=origin /old/bin/kzw-pane --pane " + PANE + " --host origin --existing\n")


class RemoteClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "server" / "workspace.sqlite")
        self.patch = patch("kzw.remote_client.call", side_effect=lambda host, req: self.store.dispatch(req))
        self.patch.start()
        self.listing = patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, PANE + "\n", ""))
        self.listing.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.listing.stop)

    def test_channel_reuses_stream_for_ordered_requests(self):
        code = ("import json,sys\nfor line in sys.stdin:\n"
                " r=json.loads(line); print(json.dumps({'ok':True,'result':r['value']}),flush=True)\n")
        channel = remote_client.Channel([sys.executable, "-u", "-c", code])
        self.addCleanup(channel.close)
        self.assertEqual(channel.request({"value": 1}), 1)
        self.assertEqual(channel.request({"value": 2}), 2)

    def test_coordinator_transport_selects_local_bridge_without_ssh(self):
        command = remote_client.coordinator_command("")
        self.assertEqual(command[:3], [sys.executable, "-u", "-c"])
        self.assertIn("KZW_LOCAL_COORDINATOR", command[3])
        with patch("kzw.remote_client.ssh_command", return_value=["ssh", "origin"]) as ssh:
            self.assertEqual(remote_client.coordinator_command("origin"), ["ssh", "origin"])
            ssh.assert_called_once_with("origin")

    def test_recreate_archives_retired_cache_without_overwriting_it(self):
        directory = self.root / "client"
        directory.mkdir()
        self.store.db.execute("INSERT INTO retired_workspaces VALUES (?,?)", ("Test", 5))
        config = {"workspace": "Test", "host": "", "revision": 4, "state_home": ""}
        (directory / "remote.json").write_text(json.dumps(config))
        (directory / "workspace.kitty-session").write_text("old layout")
        remote_client.create(directory, "Test", "")
        archive, = directory.glob(".retired-*")
        self.assertEqual((archive / "workspace.kitty-session").read_text(), "old layout")
        self.assertEqual(json.loads((archive / "remote.json").read_text()), config)
        self.assertEqual(json.loads((directory / "remote.json").read_text())["revision"], 6)

    def test_recreate_refuses_pending_cache(self):
        directory = self.root / "client"
        directory.mkdir()
        self.store.db.execute("INSERT INTO retired_workspaces VALUES (?,?)", ("Test", 5))
        config = {"workspace": "Test", "host": "", "revision": 4, "state_home": "", "pending": {"request": {}}}
        (directory / "remote.json").write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "pending"):
            remote_client.create(directory, "Test", "")
        self.assertEqual(json.loads((directory / "remote.json").read_text()), config)

    def test_channel_disconnect_is_not_replayed(self):
        channel = remote_client.Channel([sys.executable, "-u", "-c", "import sys; sys.stdin.readline()"])
        self.addCleanup(channel.close)
        with self.assertRaises(OSError):
            channel.request({"method": "workspace.put"})

    def test_channel_rejects_oversized_request(self):
        channel = remote_client.Channel([sys.executable, "-u", "-c", "import sys; sys.stdin.read()"])
        self.addCleanup(channel.close)
        with self.assertRaises(ValueError):
            channel.request({"data": "x" * remote_client.MAX_MESSAGE})

    def test_monitor_notifies_only_after_observing_live_session(self):
        directory = self.directory("monitor")
        remote_client.save_config(directory, {"host": "origin", "workspace": "Test", "state_home": ""})
        release, notified = threading.Event(), threading.Event()

        class Process:
            def poll(self):
                return None

            def terminate(self):
                release.set()

        class FakeChannel:
            def __init__(self, command):
                self.process = Process()
                self.count = 0

            def request(self, request, timeout):
                if request["method"] == "workspace.events":
                    return {"events": []}
                self.count += 1
                if self.count == 1:
                    return {"revision": 1, "live": [PANE], "missing": [], "confirmed_missing": []}
                if self.count == 2:
                    return {"revision": 1, "live": [], "missing": [PANE], "confirmed_missing": [PANE]}
                release.wait(3)
                raise OSError("closed")

            def close(self):
                pass

        original = remote_client.atomic_write
        def save(path, text):
            original(path, text)
            if json.loads(text).get("missing") == [PANE]:
                notified.set()

        with patch("kzw.remote_client.Channel", FakeChannel), \
             patch("kzw.remote_client.atomic_write", side_effect=save), \
             patch.dict("os.environ", {"KZW_REMOTE_STATE_HOME": ""}):
            stop = remote_client.start_status_monitor(directory)
            try:
                self.assertTrue(notified.wait(3))
                self.assertEqual(json.loads((directory / "remote-status.changed").read_text())["missing"], [PANE])
            finally:
                stop()

    def test_monitor_does_not_claim_unobserved_missing_session(self):
        directory = self.directory("unknown")
        remote_client.save_config(directory, {"host": "origin", "workspace": "Test", "state_home": ""})
        release, notified = threading.Event(), threading.Event()

        class Process:
            def poll(self):
                return None

            def terminate(self):
                release.set()

        class FakeChannel:
            def __init__(self, command):
                self.process = Process()
                self.count = 0

            def request(self, request, timeout):
                if request["method"] == "workspace.events":
                    return {"events": []}
                self.count += 1
                if self.count == 1:
                    return {"revision": 1, "live": [], "missing": [PANE], "confirmed_missing": []}
                release.wait(3)
                raise OSError("closed")

            def close(self):
                pass

        original = remote_client.atomic_write
        def save(path, text):
            original(path, text)
            notified.set()

        with patch("kzw.remote_client.Channel", FakeChannel), \
             patch("kzw.remote_client.atomic_write", side_effect=save), \
             patch.dict("os.environ", {"KZW_REMOTE_STATE_HOME": ""}):
            stop = remote_client.start_status_monitor(directory)
            try:
                self.assertTrue(notified.wait(3))
                self.assertEqual(json.loads((directory / "remote-status.changed").read_text())["missing"], [])
            finally:
                stop()

    def test_monitor_replays_events_after_saved_snapshot_cursor(self):
        directory = self.directory("events")
        remote_client.save_config(directory, {"host": "origin", "workspace": "Test", "state_home": "", "event_cursor": 5})
        release, notified = threading.Event(), threading.Event()
        requests = []

        class Process:
            def poll(self):
                return None

            def terminate(self):
                release.set()

        class FakeChannel:
            def __init__(self, command):
                self.process = Process()
                self.watches = 0

            def request(self, request, timeout):
                requests.append(request)
                if request["method"] == "workspace.events":
                    return {"events": [{"seq": 6, "revision": 1, "kind": "session.missing", "data": {"pane": PANE}}]}
                self.watches += 1
                if self.watches == 1:
                    return {"revision": 1, "live": [], "missing": [PANE], "confirmed_missing": [PANE]}
                release.wait(3)
                raise OSError("closed")

            def close(self):
                pass

        original = remote_client.atomic_write
        def save(path, text):
            original(path, text)
            notified.set()

        with patch("kzw.remote_client.Channel", FakeChannel), \
             patch("kzw.remote_client.atomic_write", side_effect=save), \
             patch.dict("os.environ", {"KZW_REMOTE_STATE_HOME": ""}):
            stop = remote_client.start_status_monitor(directory)
            try:
                self.assertTrue(notified.wait(3))
                notice = json.loads((directory / "remote-status.changed").read_text())
                self.assertEqual((notice["event_cursor"], notice["missing"]), (6, [PANE]))
                self.assertEqual(next(request for request in requests if request["method"] == "workspace.events")["after"], 5)
            finally:
                stop()

    def directory(self, name):
        path = self.root / name
        path.mkdir()
        return path

    def test_create_publish_pull_from_fresh_client(self):
        first = self.directory("first")
        second = self.directory("second")
        remote_client.create(first, "Test", "origin")
        self.assertEqual(self.store.dispatch({"method": "workspace.list"})["workspaces"][0]["name"], "Test")
        remote_client.publish(first, SNAPSHOT, [PANE])
        saved = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertNotIn("/old/computer", saved["snapshot"])
        self.assertNotIn("/old/bin/kzw-pane", saved["snapshot"])
        remote_client.pull(second, "Test", "origin", "/new/bin/kzw-pane")
        restored = (second / "workspace.kitty-session").read_text()
        self.assertIn("/new/bin/kzw-pane", restored)
        self.assertIn(PANE, restored)
        self.assertEqual(json.loads((second / "panes.json").read_text()), [PANE])

    def test_lost_ack_replayed_without_duplicate_revision(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        def lost_ack(host, req):
            self.store.dispatch(req)
            raise OSError("reply lost")
        with patch("kzw.remote_client.call", side_effect=lost_ack):
            with self.assertRaises(OSError):
                remote_client.publish(directory, SNAPSHOT, [PANE])
        self.assertIn("pending", json.loads((directory / "remote.json").read_text()))
        remote_client.publish(directory, SNAPSHOT, [PANE])
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["revision"], 2)

    def test_rejected_not_ready_registration_can_be_retried_with_new_snapshot(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        with patch("kzw.remote_client.call", side_effect=remote_client.RemoteRejected("not ready", "session_not_ready")):
            with self.assertRaises(remote_client.RemoteRejected):
                remote_client.publish(directory, SNAPSHOT, [PANE])
        self.assertNotIn("pending", json.loads((directory / "remote.json").read_text()))
        remote_client.publish(directory, SNAPSHOT.replace("new_tab Test", "new_tab ready"), [PANE])
        remote = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertIn("new_tab ready", remote["snapshot"])

    def test_create_retry_after_lost_ack(self):
        directory = self.directory("first")
        def call_with_lost_create(host, req):
            result = self.store.dispatch(req)
            if req["method"] == "workspace.put":
                raise OSError("reply lost")
            return result
        with patch("kzw.remote_client.call", side_effect=call_with_lost_create):
            with self.assertRaises(OSError):
                remote_client.create(directory, "Test", "origin")
        remote_client.create(directory, "Test", "origin")
        remote = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual(remote["revision"], 1)
        self.assertEqual(remote["panes"], [])

    def test_two_clients_cannot_hold_same_workspace_lease(self):
        first, second = self.directory("first"), self.directory("second")
        remote_client.create(first, "Test", "origin")
        remote_client.acquire_lease(first, "Test", "origin")
        with self.assertRaises(ValueError):
            remote_client.acquire_lease(second, "Test", "origin")
        remote_client.release_lease(first)
        remote_client.acquire_lease(second, "Test", "origin")
        remote_client.release_lease(second)

    def test_fresh_client_refuses_incomplete_remote_reservation(self):
        first, second = self.directory("first"), self.directory("second")
        remote_client.create(first, "Test", "origin")
        remote_client.acquire_lease(first, "Test", "origin")
        with patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            remote_client.reserve_pane(first, PANE, "a" * 32)
        with self.assertRaisesRegex(ValueError, "incomplete pane creation"):
            remote_client.pull(second, "Test", "origin", "/bin/kzw-pane")
        self.assertFalse((second / "workspace.kitty-session").exists())
        remote_client.release_lease(first)

    def test_lost_ack_replays_same_intent_with_new_lease(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        first = remote_client.acquire_lease(directory, "Test", "origin")
        def lost_ack(host, request):
            self.store.dispatch(request)
            raise OSError("reply lost")
        with patch("kzw.remote_client.call", side_effect=lost_ack):
            with self.assertRaises(OSError):
                remote_client.publish(directory, SNAPSHOT, [PANE])
        remote_client.release_lease(directory)
        second = remote_client.acquire_lease(directory, "Test", "origin")
        self.assertNotEqual(first, second)
        remote_client.publish(directory, SNAPSHOT, [PANE])
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["revision"], 2)

    def test_detached_retry_close_reacquires_lease_for_cleanup(self):
        from kzw.cli import retry_close
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.acquire_lease(directory, "Test", "origin")
        remote_client.publish(directory, SNAPSHOT, [PANE])
        remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_close=True, close_panes=[PANE])
        (directory / "pending-close.json").write_text(json.dumps({"phase": "layout-saved", "remote": True,
                                                                   "panes": [{"pane": PANE, "host": "origin"}]}))
        remote_client.release_lease(directory)
        listing = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("kzw.cli.state_dir", return_value=directory), \
             patch("kzw.remote.subprocess.run", return_value=listing):
            retry_close("Test", "origin")
        self.assertFalse((directory / "pending-close.json").exists())
        self.assertFalse((directory / "lease.json").exists())
        self.assertEqual(self.store.dispatch({"method": "cleanup.list", "workspace": "Test"})["panes"], [])

    def test_retry_create_publishes_reserved_pane_and_clears_journal(self):
        from kzw.cli import retry_create
        second = "kzw-" + "b" * 32
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.acquire_lease(directory, "Test", "origin")
        remote_client.publish(directory, SNAPSHOT, [PANE])
        operation = "a" * 32
        remote_client.reserve_pane(directory, second, operation)
        (directory / "panes.json").write_text(json.dumps([PANE]))
        (directory / "pending-create.json").write_text(json.dumps({"pane": second, "host": "origin", "request_id": operation}))
        tree = [{"last_focused": True, "tabs": [{"is_active": True, "windows": [
            {"id": 1, "is_active": True, "user_vars": {"kzw_pane": PANE, "kzw_host": "origin"}},
            {"id": 2, "is_active": False, "user_vars": {"kzw_pane": second, "kzw_host": "origin"}},
        ]}]}]
        export = ("new_tab Test\nlayout splits\n"
                  "launch 'kitty-unserialize-data={\"id\":1}'\n"
                  "launch 'kitty-unserialize-data={\"id\":2}'\n")
        def kitty(socket, *args):
            return export if args == ("ls", "--output-format=session") else json.dumps(tree)
        listing = subprocess.CompletedProcess([], 0, PANE + "\n" + second + "\n", "")
        with patch("kzw.cli.state_dir", return_value=directory), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.query", side_effect=kitty), \
             patch("kzw.cli.zmx_command", return_value=second + "\n"), \
             patch("kzw.remote.subprocess.run", return_value=listing):
            retry_create("Test", "origin")
        saved = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual(saved["panes"], [PANE, second])
        self.assertEqual(saved["reservations"], [])
        self.assertFalse((directory / "pending-create.json").exists())

    def test_bootstrap_reserves_before_launch_and_publishes_after_confirmation(self):
        from kzw.cli import finish_bootstrap, mark_bootstrap_launching, prepare_bootstrap
        directory = self.directory("bootstrap")
        remote_client.create(directory, "Test", "origin")
        remote_client.acquire_lease(directory, "Test", "origin")
        ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
        with patch("kzw.cli.uuid.uuid4", side_effect=ids), \
             patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            session = prepare_bootstrap(directory, "Test", "origin", "/a path/kzw-pane")
        record = json.loads((directory / "pending-create.json").read_text())
        self.assertEqual(record["pane"], PANE)
        self.assertIn("'/a path/kzw-pane' --pane " + PANE, session.read_text())
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["reservations"], [PANE])
        mark_bootstrap_launching(directory)
        tree = [{"last_focused": True, "tabs": [{"is_active": True, "windows": [
            {"id": 1, "is_active": True, "user_vars": {"kzw_pane": PANE, "kzw_host": "origin"}},
        ]}]}]
        export = "new_tab Test\nlayout splits\nlaunch 'kitty-unserialize-data={\"id\":1}'\n"
        def kitty(socket, *args):
            return export if args == ("ls", "--output-format=session") else json.dumps(tree)
        with patch("kzw.cli.query", side_effect=kitty), patch("kzw.cli.zmx_command", return_value=""):
            with self.assertRaisesRegex(ValueError, "waiting"):
                finish_bootstrap(directory, "/a path/kzw-pane")
        self.assertTrue((directory / "pending-create.json").exists())
        self.assertTrue(session.exists())
        with patch("kzw.cli.query", side_effect=kitty), patch("kzw.cli.zmx_command", return_value=PANE + "\n"):
            finish_bootstrap(directory, "/a path/kzw-pane")
        state = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual(state["panes"], [PANE])
        self.assertEqual(state["reservations"], [])
        self.assertFalse((directory / "pending-create.json").exists())
        self.assertFalse(session.exists())
        remote_client.release_lease(directory)

    def test_bootstrap_recovery_never_recreates_uncertain_session(self):
        from kzw.cli import mark_bootstrap_launching, prepare_bootstrap, recover_bootstrap
        directory = self.directory("bootstrap-recovery")
        remote_client.create(directory, "Test", "origin")
        remote_client.acquire_lease(directory, "Test", "origin")
        ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
        with patch("kzw.cli.uuid.uuid4", side_effect=ids), \
             patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            session = prepare_bootstrap(directory, "Test", "origin", "/bin/kzw-pane")
        with patch("kzw.cli.zmx_command", return_value=""):
            self.assertEqual(recover_bootstrap(directory, "Test", "origin", "/bin/kzw-pane"), session)
        self.assertNotIn("--existing", session.read_text())
        mark_bootstrap_launching(directory)
        with patch("kzw.cli.zmx_command", return_value=""):
            with self.assertRaisesRegex(ValueError, "refusing to recreate"):
                recover_bootstrap(directory, "Test", "origin", "/bin/kzw-pane")
        with patch("kzw.cli.zmx_command", return_value=PANE + "\n"):
            recover_bootstrap(directory, "Test", "origin", "/bin/kzw-pane")
        self.assertIn("--existing", session.read_text())
        self.assertEqual(json.loads((directory / "pending-create.json").read_text())["phase"], "launching")
        remote_client.release_lease(directory)

    def test_bootstrap_reservation_reply_loss_is_replayable(self):
        from kzw.cli import prepare_bootstrap, recover_bootstrap
        directory = self.directory("bootstrap-lost")
        remote_client.create(directory, "Test", "origin")
        remote_client.acquire_lease(directory, "Test", "origin")
        ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
        original = remote_client.reserve_pane
        def lost(*args):
            original(*args)
            raise OSError("reply lost")
        with patch("kzw.cli.uuid.uuid4", side_effect=ids), \
             patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch("kzw.cli.remote_client.reserve_pane", side_effect=lost):
            with self.assertRaises(OSError):
                prepare_bootstrap(directory, "Test", "origin", "/bin/kzw-pane")
        self.assertEqual(json.loads((directory / "pending-create.json").read_text())["phase"], "reserving")
        with patch("kzw.cli.zmx_command", return_value=""):
            session = recover_bootstrap(directory, "Test", "origin", "/bin/kzw-pane")
        self.assertTrue(session.is_file())
        self.assertEqual(json.loads((directory / "pending-create.json").read_text())["phase"], "reserved")
        remote_client.release_lease(directory)

    def test_attach_recovers_reserved_bootstrap_under_new_lease(self):
        from kzw.cli import attach_remote, prepare_bootstrap
        directory = self.directory("bootstrap-attach")
        remote_client.create(directory, "Test", "origin")
        remote_client.acquire_lease(directory, "Test", "origin")
        ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
        with patch("kzw.cli.uuid.uuid4", side_effect=ids), \
             patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            session = prepare_bootstrap(directory, "Test", "origin", "/bin/kzw-pane")
        remote_client.release_lease(directory)
        with patch("kzw.cli.state_dir", return_value=directory), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.zmx_command", return_value=""), \
             patch("kzw.cli.run", return_value=0) as run:
            self.assertEqual(attach_remote("Test", "origin"), 0)
        self.assertEqual(run.call_args.kwargs["bootstrap_session"], session)
        self.assertFalse((directory / "lease.json").exists())
        self.assertTrue((directory / "pending-create.json").exists())

    def test_bootstrap_recovers_committed_layout_after_lost_ack(self):
        from kzw.cli import mark_bootstrap_launching, prepare_bootstrap, reconcile_bootstrap_commit
        directory = self.directory("bootstrap-ack")
        remote_client.create(directory, "Test", "origin")
        remote_client.acquire_lease(directory, "Test", "origin")
        ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
        with patch("kzw.cli.uuid.uuid4", side_effect=ids), \
             patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            prepare_bootstrap(directory, "Test", "origin", "/bin/kzw-pane")
        mark_bootstrap_launching(directory)
        def lost_ack(host, request):
            self.store.dispatch(request)
            raise OSError("reply lost")
        with patch("kzw.remote_client.call", side_effect=lost_ack):
            with self.assertRaises(OSError):
                remote_client.publish(directory, SNAPSHOT, [PANE])
        self.assertIn("pending", json.loads((directory / "remote.json").read_text()))
        self.assertTrue(reconcile_bootstrap_commit(directory, "Test", "origin", "/bin/kzw-pane"))
        self.assertFalse((directory / "pending-create.json").exists())
        self.assertFalse((directory / "bootstrap.kitty-session").exists())
        self.assertIn(PANE, (directory / "workspace.kitty-session").read_text())
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["revision"], 2)
        remote_client.release_lease(directory)

    def test_explicit_empty_workspace_bootstrap_preserves_empty_until_published(self):
        from kzw.cli import EMPTY_SNAPSHOT, prepare_bootstrap, recover_bootstrap
        directory = self.directory("empty-bootstrap")
        remote_client.create(directory, "Test", "origin")
        remote_client.publish(directory, EMPTY_SNAPSHOT, [])
        (directory / "workspace.kitty-session").write_text(EMPTY_SNAPSHOT)
        (directory / "panes.json").write_text("[]\n")
        remote_client.acquire_lease(directory, "Test", "origin")
        ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
        with patch("kzw.cli.uuid.uuid4", side_effect=ids), \
             patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            session = prepare_bootstrap(directory, "Test", "origin", "/bin/kzw-pane", allow_empty=True)
        self.assertEqual((directory / "workspace.kitty-session").read_text(), EMPTY_SNAPSHOT)
        self.assertEqual(json.loads((directory / "pending-create.json").read_text())["starting_snapshot"], "empty")
        with patch("kzw.cli.zmx_command", return_value=""):
            self.assertEqual(recover_bootstrap(directory, "Test", "origin", "/bin/kzw-pane"), session)
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["snapshot"], EMPTY_SNAPSHOT)
        remote_client.release_lease(directory)

    def test_stale_revision_conflict_does_not_replace_remote(self):
        first = self.directory("first")
        second = self.directory("second")
        remote_client.create(first, "Test", "origin")
        remote_client.pull(second, "Test", "origin", "/bin/kzw-pane")
        remote_client.publish(first, SNAPSHOT, [PANE])
        with self.assertRaises(ValueError):
            remote_client.publish(second, SNAPSHOT.replace("new_tab Test", "new_tab stale"), [PANE])
        remote = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertNotIn("stale", remote["snapshot"])

    def test_revision_check_refuses_stale_view_mutation(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.ensure_current_revision(directory)
        self.store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "newer",
                             "revision": 1, "snapshot": "newer snapshot", "panes": []})
        with self.assertRaisesRegex(ValueError, "revision changed"):
            remote_client.ensure_current_revision(directory)

    def test_reject_mixed_host_and_foreign_launch(self):
        with self.assertRaises(ValueError):
            remote_client.translate(SNAPSHOT, "", "different", True)
        with self.assertRaises(ValueError):
            remote_client.translate("launch echo dangerous\n", "/bin/kzw-pane", "origin", False)

    def test_existing_local_workspace_is_never_overwritten(self):
        directory = self.directory("first")
        (directory / "workspace.kitty-session").write_text("existing")
        with self.assertRaises(ValueError):
            remote_client.create(directory, "Test", "origin")
        self.assertEqual((directory / "workspace.kitty-session").read_text(), "existing")

    def test_prune_requires_confirmed_absence_on_origin(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.publish(directory, SNAPSHOT, [PANE])
        live = subprocess.CompletedProcess([], 0, stdout=PANE + "\n", stderr="")
        with patch("kzw.remote.subprocess.run", return_value=live):
            with self.assertRaises(ValueError):
                remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_exits=True)
        self.assertTrue(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"])
        absent = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("kzw.remote.subprocess.run", return_value=absent):
            remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_exits=True)
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [])

    def test_prune_listing_failure_preserves_remote_manifest(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.publish(directory, SNAPSHOT, [PANE])
        with patch("kzw.remote.subprocess.run", side_effect=OSError("zmx unavailable")):
            with self.assertRaises(OSError):
                remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_exits=True)
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [PANE])

    def test_confirmed_close_lost_ack_replays_and_schedules_cleanup(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.publish(directory, SNAPSHOT, [PANE])
        def lost_ack(host, req):
            self.store.dispatch(req)
            raise OSError("reply lost")
        with patch("kzw.remote_client.call", side_effect=lost_ack):
            with self.assertRaises(OSError):
                remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_close=True, close_panes=[PANE])
        remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_close=True, close_panes=[PANE])
        self.assertEqual(self.store.dispatch({"method": "cleanup.list", "workspace": "Test"})["panes"], [PANE])
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["revision"], 3)

    def test_cleanup_failure_is_not_acknowledged(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.publish(directory, SNAPSHOT, [PANE])
        remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_close=True, close_panes=[PANE])
        live = subprocess.CompletedProcess([], 0, stdout=PANE + "\n", stderr="")
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="failed")
        with patch("kzw.remote.subprocess.run", side_effect=[live, failed]):
            with self.assertRaises(ValueError):
                remote_client.retry_cleanup(directory)
        self.assertEqual(self.store.dispatch({"method": "cleanup.list", "workspace": "Test"})["panes"], [PANE])
