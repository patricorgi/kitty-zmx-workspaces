import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kzw import cli, connection, network, pane, remote_client, shell_cwd, ssh_transport, titles
from kzw.attachment import Attachment

PANE = "kzw-" + "a" * 32


class GateTests(unittest.TestCase):
    def test_offline_manual_retry_cannot_bypass_pause_and_online_skips_backoff(self):
        gate = network.Gate(False)
        done, wake, stop = threading.Event(), threading.Event(), threading.Event()
        result = []

        def wait():
            result.append(gate.wait_retry(30, wake, stop))
            done.set()

        thread = threading.Thread(target=wait)
        thread.start()
        try:
            wake.set()
            self.assertFalse(done.wait(0.05))
            gate.update(True)
            self.assertTrue(done.wait(1), "network notification did not bypass retry delay")
            self.assertEqual(result, [True])
        finally:
            stop.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_restoration_during_inflight_timeout_is_not_lost(self):
        gate = network.Gate(True)
        before = gate.resumes
        gate.update(False)
        gate.update(True)
        self.assertTrue(gate.wait_retry(30, threading.Event(), since=before))
        self.assertEqual(gate.resumes, 1)
        gate.update(True)
        self.assertEqual(gate.resumes, 1)

    def test_shutdown_does_not_wait_for_network(self):
        gate = network.Gate(False)
        stop, entered, done = threading.Event(), threading.Event(), threading.Event()
        result = []

        def wait():
            entered.set()
            result.append(gate.wait_online(stop))
            done.set()

        thread = threading.Thread(target=wait)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            stop.set()
            self.assertTrue(done.wait(1))
            self.assertEqual(result, [False])
        finally:
            stop.set()
            thread.join(1)

    def test_unknown_and_local_are_not_falsely_fenced(self):
        network.Gate(None).require("origin")
        network.Gate(False).require("")
        with self.assertRaises(network.OfflineError):
            network.Gate(False).require("origin")

    def test_callbacks_only_observe_transitions_and_unsubscribe(self):
        gate = network.Gate(None)
        callback = Mock()
        unsubscribe = gate.subscribe(callback)
        gate.update(False)
        gate.update(False)
        gate.update(True)
        self.assertEqual([c.args for c in callback.call_args_list], [(None, False), (False, True)])
        unsubscribe()
        gate.update(False)
        self.assertEqual(callback.call_count, 2)

    def test_reachability_flags_do_not_mistake_a_required_manual_connection_for_online(self):
        for flags, available in ((0, False), (2, True), (2 | 4, False), (2 | 4 | 8, True),
                                 (2 | 4 | 32, True), (2 | 4 | 8 | 16, False), (8 | 32, False)):
            with self.subTest(flags=flags):
                self.assertEqual(network.usable(flags), available)


class NetworkHealthTests(unittest.TestCase):
    def test_resume_only_interrupts_affected_transport_and_manual_retry_cannot_override_offline(self):
        now = [100]
        health = connection.Health(lambda: now[0])
        gate = network.Gate(True)
        health.bind_network(gate)
        self.addCleanup(health.unsubscribe)
        bad, healthy = Mock(), Mock()
        bad.process.poll.return_value = healthy.process.poll.return_value = None
        bad_wake, healthy_wake = threading.Event(), threading.Event()
        health.begin("status", bad, 8, bad_wake)
        health.begin("lease", healthy, 100, healthy_wake)
        gate.update(False)
        now[0] += 10
        self.assertEqual(health.state(), "offline")
        health.retry()
        bad.process.terminate.assert_not_called()
        self.assertFalse(bad_wake.is_set())
        gate.update(True)
        bad.process.terminate.assert_called_once()
        healthy.process.terminate.assert_not_called()
        self.assertTrue(bad_wake.is_set())
        self.assertTrue(healthy_wake.is_set())
        self.assertEqual(health.state(), "reconnecting")

    def test_gate_blocks_every_background_ssh_entry_point_without_changing_pending_state(self):
        gate = network.Gate(False)
        with patch.object(network, "_gate", gate), patch("subprocess.Popen") as popen, \
             patch("subprocess.run") as run:
            actions = [lambda: remote_client.direct_call("origin", {"method": "workspace.get", "workspace": "Test"}),
                       lambda: remote_client.Channel(["ssh", "origin"]),
                       lambda: titles.read_host("origin"), lambda: shell_cwd.from_session(PANE, "origin"),
                       lambda: shell_cwd.resolve("file://origin/home/test", "origin"),
                       lambda: cli.zmx_command("origin", "list", "--short"),
                       lambda: pane.run_attachment(["ssh", "-t", "origin", "command"], {}, True)]
            for action in actions:
                with self.assertRaises(network.OfflineError):
                    action()
            with remote_client.persistent_rpc(), self.assertRaises(network.OfflineError):
                remote_client.direct_call("origin", {"method": "workspace.put", "workspace": "Test"})
            popen.assert_not_called()
            run.assert_not_called()

    def test_offline_attachment_never_preflights_or_spawns_and_keeps_initial_intent(self):
        gate = network.Gate(False)
        with patch.object(network, "_gate", gate), patch("kzw.attachment.user_var"), \
             patch("kzw.attachment.threading.Thread"), patch("kzw.attachment.RetryServer") as server:
            server.return_value.identity = {}
            attachment = Attachment("Test", "origin", PANE, existing=False)
            try:
                self.assertEqual(attachment.phase, "offline")
                self.assertEqual(attachment.initial_phase, "connecting")
                with patch("kzw.ssh_transport.prepare_attachment") as prepare, \
                     patch("kzw.attachment.subprocess.Popen") as popen, self.assertRaises(network.OfflineError):
                    attachment.run(["ssh", "-t", "origin", "command"], {})
                prepare.assert_not_called()
                popen.assert_not_called()
                attachment.retry()
                self.assertFalse(attachment.wake.is_set())
            finally:
                attachment.close()

    def test_network_events_preserve_healthy_pane_and_wake_only_stalled_attachment(self):
        for healthy in (False, True):
            gate = network.Gate(True)
            with self.subTest(healthy=healthy), patch.object(network, "_gate", gate), \
                 patch("kzw.attachment.user_var"), patch("kzw.attachment.threading.Thread"), \
                 patch("kzw.attachment.RetryServer") as server:
                server.return_value.identity = {}
                attachment = Attachment("Test", "origin", PANE, existing=True)
                try:
                    child = Mock()
                    child.poll.return_value = None
                    attachment.process = child
                    attachment.phase = "" if healthy else "stalled"
                    gate.update(False)
                    attachment.stop.wait = Mock(side_effect=[False, True])
                    attachment.monitor()
                    child.terminate.assert_not_called()
                    self.assertEqual(attachment.phase, "" if healthy else "offline")
                    gate.update(True)
                    attachment.stop.wait = Mock(side_effect=[False, True])
                    attachment.monitor()
                    if healthy:
                        child.terminate.assert_not_called()
                        self.assertFalse(attachment.wake.is_set())
                    else:
                        child.terminate.assert_called_once()
                        self.assertTrue(attachment.wake.is_set())
                finally:
                    attachment.close()

    def test_master_setup_cannot_fall_through_to_a_network_attempt_while_offline(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(network, "_gate", network.Gate(False)), \
             patch("kzw.ssh_transport.subprocess.run") as run:
            env = {"XDG_CACHE_HOME": tmp, "KZW_WORKSPACE": "Test", "KZW_WORKSPACE_DIRECTORY": "/test"}
            command = ["ssh", *ssh_transport.pane_options("origin", env), "-t", "origin", "command"]
            prepared = ssh_transport.prepare_attachment(command, env)
            self.assertIn("ControlPath=none", prepared)
            run.assert_not_called()


class MonitorGateTests(unittest.TestCase):
    def write_state(self, directory):
        lease = {"host": "origin", "workspace": "Test", "token": "a" * 32, "state_home": ""}
        (directory / "lease.json").write_text(json.dumps(lease))
        config = {"host": "origin", "workspace": "Test", "state_home": "", "revision": 1,
                  "pending": {"request": {"request_id": "durable", "method": "workspace.put"}}}
        (directory / "remote.json").write_text(json.dumps(config))
        return (directory / "lease.json").read_bytes(), (directory / "remote.json").read_bytes()

    def test_local_workspace_heartbeat_is_not_paused_by_offline_network(self):
        renewed = threading.Event()
        channel = Mock()
        channel.process.poll.return_value = None
        channel.request.side_effect = lambda request, timeout: renewed.set() or {}
        with tempfile.TemporaryDirectory() as tmp, patch.object(network, "_gate", network.Gate(False)), \
             patch("kzw.remote_client.Channel", return_value=channel):
            directory = Path(tmp)
            (directory / "lease.json").write_text(json.dumps({"host": "", "workspace": "Test", "token": "a" * 32}))
            close = remote_client.start_lease_heartbeat(directory)
            try:
                self.assertTrue(renewed.wait(1))
                self.assertNotEqual(connection.for_directory(directory).state(), "offline")
            finally:
                close()

    def test_lease_and_status_make_zero_attempts_offline_then_both_wake_immediately(self):
        gate = network.Gate(False)
        renewed, watched = threading.Event(), threading.Event()
        channels = []

        class Channel:
            def __init__(self, command):
                self.cancel = threading.Event()
                self.process = Mock()
                self.process.poll.return_value = None
                self.process.terminate.side_effect = self.cancel.set
                self.watches = 0
                channels.append(self)

            def request(self, request, timeout):
                if request["method"] == "workspace.lease.renew":
                    renewed.set()
                    return {}
                if request["method"] == "workspace.events":
                    return {"events": []}
                self.watches += 1
                if self.watches == 1:
                    return {"revision": 1, "live": [PANE], "missing": [], "confirmed_missing": []}
                self.cancel.wait(3)
                raise OSError("closed disposable stream")

            def close(self):
                self.cancel.set()

        with tempfile.TemporaryDirectory() as tmp, patch.object(network, "_gate", gate), \
             patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": ""}), \
             patch("kzw.remote_client.Channel", Channel):
            directory = Path(tmp)
            before = self.write_state(directory)
            stop_lease = remote_client.start_lease_heartbeat(directory)
            stop_status = remote_client.start_status_monitor(directory, on_metrics=lambda sample: watched.set())
            try:
                self.assertFalse(renewed.wait(0.05))
                self.assertEqual(channels, [])
                connection.for_directory(directory).retry()
                self.assertEqual(channels, [])
                gate.update(True)
                self.assertTrue(renewed.wait(1))
                self.assertTrue(watched.wait(1))
                self.assertEqual(len(channels), 2)
                for channel in channels:
                    channel.process.terminate.assert_not_called()
                self.assertEqual(((directory / "lease.json").read_bytes(),
                                  (directory / "remote.json").read_bytes()), before)
            finally:
                stop_status()
                stop_lease()

    def test_failed_lease_wait_pauses_offline_and_restoration_bypasses_five_second_delay(self):
        gate = network.Gate(True)
        failed, renewed = threading.Event(), threading.Event()
        count = []

        class Channel:
            def __init__(self, command):
                self.index = len(count)
                count.append(self)
                self.process = Mock()
                self.process.poll.return_value = None

            def request(self, request, timeout):
                if self.index == 0:
                    gate.update(False)
                    failed.set()
                    raise OSError("simulated network loss")
                renewed.set()
                return {}

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp, patch.object(network, "_gate", gate), \
             patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": ""}), \
             patch("kzw.remote_client.Channel", Channel), patch("sys.stderr"):
            directory = Path(tmp)
            before = self.write_state(directory)
            close = remote_client.start_lease_heartbeat(directory)
            try:
                self.assertTrue(failed.wait(1))
                self.assertFalse(renewed.wait(0.05))
                self.assertEqual(len(count), 1)
                gate.update(True)
                self.assertTrue(renewed.wait(1))
                self.assertEqual(len(count), 2)
                self.assertEqual(((directory / "lease.json").read_bytes(),
                                  (directory / "remote.json").read_bytes()), before)
            finally:
                close()


class ObserverTests(unittest.TestCase):
    def test_observer_failure_becomes_unknown_instead_of_permanently_offline(self):
        gate = network.Gate(False)
        monitor = network.DarwinMonitor(gate)
        with patch("kzw.network.ctypes.CDLL", side_effect=OSError("not available")):
            monitor.start()
            monitor.close()
        self.assertIsNone(gate.available)
        self.assertIn("not available", monitor.error)
        self.assertFalse(monitor.thread.is_alive())

    @unittest.skipUnless(sys.platform == "darwin" and os.environ.get("KZW_RUN_NETWORK_OBSERVER_INTEGRATION") == "1",
                         "native macOS observer smoke test is opt-in and read-only")
    def test_native_observer_registration_initial_sample_and_cleanup(self):
        monitor = network.DarwinMonitor(network.Gate())
        monitor.start()
        try:
            self.assertIsNone(monitor.error)
            self.assertIs(type(monitor.gate.available), bool)
            self.assertTrue(monitor.thread.is_alive())
        finally:
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())
