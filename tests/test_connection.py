import importlib.util
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from kzw import connection, remote_client
from kzw.attachment import Attachment, ready_frame

PANE = "kzw-" + "a" * 32
OTHER = "kzw-" + "b" * 32


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.health = connection.Health(lambda: self.now)
        self.channel = Mock()
        self.channel.process.poll.return_value = None
        self.wake = threading.Event()

    def test_no_optimistic_success_and_stale_activity_is_independent_of_rpc(self):
        self.health.begin("status", self.channel, 8, self.wake, idle_after=8)
        self.assertEqual(self.health.state(), "restoring")
        self.now = 109
        self.assertEqual(self.health.state(), "stalled")
        self.health.success("status")
        self.assertEqual(self.health.state(), "")
        self.now = 118
        self.assertEqual(self.health.state(), "stalled")

    def test_retry_is_scoped_and_coalesced_and_healthy_channels_are_not_cancelled(self):
        healthy = Mock()
        self.health.begin("status", self.channel, 8, self.wake)
        self.health.begin("lease", healthy, 20, threading.Event())
        other = connection.Health(lambda: self.now)
        other.begin("status", Mock(), 8, threading.Event())
        self.now = 109
        self.health.retry()
        self.health.retry()
        self.assertEqual(self.health.state(), "reconnecting")
        self.channel.process.terminate.assert_called_once()
        healthy.process.terminate.assert_not_called()
        other.sources["status"]["channel"].process.terminate.assert_not_called()
        self.assertTrue(self.wake.is_set())

    def test_failed_wait_is_woken_but_a_new_attempt_is_not_repeatedly_interrupted(self):
        self.health.begin("status", self.channel, 8, self.wake)
        self.health.failed("status")
        self.health.retry()
        self.assertTrue(self.wake.is_set())
        self.wake.clear()
        self.health.begin("status", self.channel, 8, self.wake)
        self.now += 2
        self.health.retry()
        self.channel.process.terminate.assert_not_called()
        self.assertFalse(self.wake.is_set())
        self.health.success("status")
        self.assertEqual(self.health.state(), "")

    def test_all_control_sources_must_recover(self):
        for source in ("status", "lease"):
            self.health.begin(source, self.channel, 8, self.wake)
            self.health.failed(source)
        self.health.success("status")
        self.assertEqual(self.health.state(), "reconnecting")
        self.health.success("lease")
        self.assertEqual(self.health.state(), "")

    def test_idle_rpc_does_not_become_stale(self):
        self.health.begin("rpc", self.channel, 8, self.wake)
        self.health.success("rpc")
        self.now += 10000
        self.assertEqual(self.health.state(), "")

    def test_cancelled_publication_retains_exact_durable_operation(self):
        snapshot = ("new_tab Test\nlayout splits\nlaunch 'kitty-unserialize-data={\"id\":1}' /bin/kzw pane --pane " + PANE +
                    " --host origin --existing\n")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            remote_client.save_config(directory, {"host": "origin", "workspace": "Test", "revision": 1,
                                                  "state_home": "", "panes": [PANE]})
            sent = []

            def cancel(host, request):
                sent.append(dict(request))
                self.health.begin("rpc", self.channel, 8, self.wake)
                self.now += 9
                self.health.retry()
                raise OSError("connection cancelled; outcome unknown")

            with patch("kzw.remote_client.call", side_effect=cancel), \
                 self.assertRaises(OSError):
                remote_client.publish(directory, snapshot, [PANE])
            pending = json.loads((directory / "remote.json").read_text())["pending"]["request"]
            self.assertEqual(pending, sent[0])
            self.assertEqual(len(sent), 1)
            self.assertEqual(pending["panes"], [PANE])
            self.assertIn("request_id", pending)


class EndpointTests(unittest.TestCase):
    def test_private_endpoint_checks_full_identity_and_cleans_only_itself(self):
        called = threading.Event()
        server = connection.RetryServer("Test", "origin", called.set, PANE, "/one")
        try:
            self.assertEqual(server.directory.stat().st_mode & 0o777, 0o700)
            for args in (("Other", "origin", PANE, "/one"), ("Test", "elsewhere", PANE, "/one"),
                         ("Test", "origin", OTHER, "/one"), ("Test", "origin", PANE, "/two")):
                self.assertFalse(connection.request_retry(server.identity, *args))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(server.path))
                client.sendall(json.dumps({**server.identity, "token": "wrong"}).encode() + b"\n")
            self.assertFalse(called.wait(0.05))
            self.assertTrue(connection.request_retry(server.identity, "Test", "origin", PANE, "/one"))
            self.assertTrue(called.wait(1))
        finally:
            server.close()
        self.assertFalse(server.directory.exists())


class AttachmentTests(unittest.TestCase):
    def test_transport_completion_requires_origin_readiness(self):
        popen = subprocess.Popen
        for ready in (False, True):
            with self.subTest(ready=ready), patch("kzw.attachment.user_var"):
                attachment = Attachment("Test", "origin", PANE)
                attachment.direct = True
                try:
                    data = attachment.marker if ready else b""
                    def launch(_command, **options):
                        return popen([sys.executable, "-c", f"import os; os.write(1, {data!r}); raise SystemExit(255)"],
                                     **options)
                    with patch("kzw.attachment.subprocess.Popen", side_effect=launch):
                        self.assertEqual(attachment.run(["ssh", "-t", "origin", "command"], {}).returncode, 255)
                    self.assertIs(attachment.transport_ended, ready)
                finally:
                    attachment.close()

    def test_new_pane_connects_and_existing_pane_restores_including_transport_start(self):
        endpoint = types.SimpleNamespace(identity={}, close=Mock())
        for existing, expected in ((False, "connecting"), (True, "restoring")):
            with self.subTest(existing=existing), patch("kzw.attachment.RetryServer", return_value=endpoint), \
                 patch("kzw.attachment.threading.Thread"), patch("kzw.attachment.user_var") as variable:
                attachment = Attachment("Test", "origin", PANE, existing=existing)
                try:
                    self.assertEqual(attachment.phase, expected)
                    variable.assert_any_call("kzw_connection", expected)
                    attachment.direct = True
                    with patch("kzw.attachment.subprocess.Popen", side_effect=OSError("unavailable")), \
                         self.assertRaises(OSError):
                        attachment.run(["ssh", "-t", "origin", "command"], {})
                    self.assertEqual(attachment.phase, expected)
                    self.assertEqual(variable.call_args.args, ("kzw_connection", expected))
                    attachment.stop.wait = Mock(side_effect=[False, True])
                    with patch("kzw.attachment.time.monotonic", return_value=attachment.started + 9):
                        attachment.monitor()
                    self.assertEqual(attachment.phase, "stalled")
                finally:
                    attachment.close()

    def test_new_pane_retries_restore_only_after_a_transport_was_attempted(self):
        endpoint = types.SimpleNamespace(identity={}, close=Mock())
        with patch("kzw.attachment.RetryServer", return_value=endpoint), \
             patch("kzw.attachment.threading.Thread"), patch("kzw.attachment.user_var"):
            attachment = Attachment("Test", "origin", PANE, existing=False)
            try:
                attachment.ended()  # e.g. a failed lease check before starting SSH
                self.assertEqual(attachment.initial_phase, "connecting")
                attachment.process = Mock()
                attachment.ended()  # SSH ran; its remote outcome may be unknown
                self.assertEqual(attachment.initial_phase, "restoring")
                self.assertEqual(attachment.phase, "reconnecting")
            finally:
                attachment.close()

    def test_acknowledgement_is_framed_across_every_split_without_altering_terminal_bytes(self):
        marker = b"\x1b]kzw-ready;nonce\x07"
        before, after = b"\xff\x1b[31mhello", "中文\x1b[0m".encode()
        wire = before + marker + after
        for split in range(len(wire) + 1):
            first, pending, ready = ready_frame(wire[:split], marker)
            second, tail, ready = ready_frame(pending + wire[split:], marker, ready)
            self.assertEqual(first + second + tail, before + after)
            self.assertTrue(ready)
        self.assertEqual(ready_frame(b"normal output", marker), (b"normal output", b"", False))
        self.assertFalse(ready_frame(b"kzw-ready;different-nonce", marker)[2])

    def test_only_failed_or_stalled_local_child_can_be_cancelled(self):
        with patch("kzw.attachment.user_var"):
            attachment = Attachment("Test", "origin", PANE)
            try:
                child = Mock()
                child.poll.return_value = None
                attachment.process = child
                for phase in ("", "connecting", "restoring"):
                    attachment.phase = phase
                    attachment.retry()
                    child.terminate.assert_not_called()
                attachment.phase = "stalled"
                attachment.retry()
                attachment.retry()
                child.terminate.assert_called_once()
                self.assertTrue(attachment.direct)
                self.assertTrue(attachment.wake.is_set())
                self.assertEqual(attachment.phase, "reconnecting")
            finally:
                attachment.close()

    def test_kitten_retry_signals_the_panes_process_group(self):
        with patch("kzw.attachment.user_var"), patch("kzw.attachment.os.killpg") as kill:
            attachment = Attachment("Test", "origin", PANE)
            child = Mock()
            child.poll.return_value = None
            attachment.process = child
            attachment.process_group = 1234
            try:
                attachment.phase = "stalled"
                attachment.retry()
                kill.assert_called_once_with(1234, signal.SIGTERM)
                child.terminate.assert_not_called()
                self.assertTrue(attachment.direct)
            finally:
                attachment.ended()
                attachment.close()


def load_retry_kitten():
    handler = types.ModuleType("kittens.tui.handler")
    handler.result_handler = lambda **kw: lambda fn: fn
    with patch.dict(sys.modules, {"kittens.tui.handler": handler}):
        spec = importlib.util.spec_from_file_location("retry_test", Path(__file__).parents[1] / "kzw/retry_kitten.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class RetryKittenTests(unittest.TestCase):
    def setUp(self):
        self.module = load_retry_kitten()
        self.windows = []
        for pane, state in ((PANE, "reconnecting"), (OTHER, "")):
            self.windows.append(types.SimpleNamespace(os_window_id=1, user_vars={
                "kzw_workspace": "Test", "kzw_origin": "origin", "kzw_directory": "/one", "kzw_pane": pane,
                "kzw_connection": state, "kzw_retry_endpoint": json.dumps({"pane": pane})}))
        self.manager = types.SimpleNamespace(tabs=[self.windows], mark_tab_bar_dirty=Mock(),
                                             _kzw_resource_usage={"workspace": "Test", "origin": "origin",
                                                                  "directory": "/one", "retry": {"pane": ""}})
        self.boss = types.SimpleNamespace(window_id_map={3: self.windows[0]}, os_window_map={1: self.manager})

    def test_only_affected_panes_in_current_os_window_are_requested(self):
        with patch.object(self.module, "request_retry", return_value=True) as retry:
            self.assertTrue(self.module.handle_result([], None, 3, self.boss))
        self.assertEqual([call.args[0] for call in retry.call_args_list], [{"pane": ""}, {"pane": PANE}])
        self.assertTrue(all(call.args[1:3] == ("Test", "origin") for call in retry.call_args_list))

    def test_mixed_workspace_origin_or_directory_fails_closed(self):
        for key in ("kzw_workspace", "kzw_origin", "kzw_directory"):
            previous = self.windows[1].user_vars[key]
            self.windows[1].user_vars[key] = "Other"
            with patch.object(self.module, "request_retry") as retry:
                self.assertFalse(self.module.handle_result([], None, 3, self.boss))
                retry.assert_not_called()
            self.windows[1].user_vars[key] = previous

    def test_unmanaged_and_old_panes_are_not_process_signal_targets(self):
        self.windows[0].user_vars.clear()
        with patch.object(self.module, "request_retry") as retry:
            self.assertFalse(self.module.handle_result([], None, 3, self.boss))
            retry.assert_not_called()
