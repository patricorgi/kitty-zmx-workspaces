import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from kzw import remote_client


class BrokerTests(unittest.TestCase):
    def test_inventory_queries_use_existing_transport_but_observations_are_fresh(self):
        def observation(generation):
            return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {"generation": generation}}))

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(remote_client.subprocess, "run", side_effect=[observation(1), observation(2)]) as run:
            for generation in (1, 2):
                self.assertEqual(remote_client.session_inventory("origin"), {"generation": generation})
        self.assertEqual(run.call_count, 2)
        for request in run.call_args_list:
            self.assertEqual(json.loads(request.kwargs["input"]), {"method": "session.inventory"})
            self.assertIn("ControlMaster=no", request.args[0])
            self.assertIn("ClearAllForwardings=yes", request.args[0])
            self.assertNotIn("ControlPath=none", request.args[0])

    def test_remote_read_commands_use_existing_master_without_starting_one(self):
        from kzw import cli
        snap = {"exists": True, "snapshot": "# kzw-empty-workspace\n", "panes": [],
                "revision": 1, "event_cursor": 2}
        for args, result in ((["list"], {"workspaces": [{"name": "Test"}]}), (["inspect", "Test", "--json"], snap)):
            for enabled in ("0", "1"):
                reply = json.dumps({"ok": True, "result": result})
                with self.subTest(args=args, enabled=enabled), \
                     patch.dict(os.environ, {"KZW_ATTACH_REUSE_SSH": enabled}, clear=True), \
                     patch("sys.argv", ["kzw", *args, "--host", "origin"]), \
                     patch("sys.stdout", new_callable=io.StringIO) as output, \
                     patch.object(remote_client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, reply)) as run:
                    self.assertEqual(cli.main(), 0)
                self.assertEqual(run.call_count, 1)
                command = run.call_args.args[0]
                self.assertIn("ControlMaster=no", command)
                self.assertIn("ControlPersist=no", command)
                self.assertEqual("ControlPath=none" in command, enabled == "0")
                self.assertEqual("ClearAllForwardings=yes" in command, enabled == "1")
                if args == ["list"]:
                    self.assertEqual(output.getvalue(), "Test\n")
                else:
                    self.assertEqual(json.loads(output.getvalue()), {**snap, "workspace": "Test", "origin": "origin",
                                                                    "focused_pane": None})

    def test_transport_errors_are_bounded_and_never_replay_or_print_shipped_source(self):
        command = ["ssh", "origin", "encoded-private-coordinator-source"]
        failures = (subprocess.CalledProcessError(1, command, stderr="traceback\nserver disconnected"),
                    subprocess.TimeoutExpired(command, 30))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                 patch("kzw.remote_client.coordinator_command", return_value=command), \
                 patch("kzw.remote_client.subprocess.run", side_effect=failure) as run:
                with self.assertRaises(OSError) as caught:
                    remote_client.direct_call("origin", {"method": "workspace.put"})
                self.assertIn("operation outcome may be unknown", str(caught.exception))
                self.assertNotIn(command[-1], str(caught.exception))
                run.assert_called_once()

    def test_shares_supervisor_connection_and_checks_origin(self):
        channel = Mock()
        channel.request.side_effect = lambda request: {"method": request["method"]}
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}, clear=True), \
             patch("kzw.remote_client.Channel", return_value=channel) as factory:
            with remote_client.persistent_rpc():
                self.assertEqual(remote_client.direct_call("origin", {"method": "first"}), {"method": "first"})
                broker = remote_client.ConnectionBroker("origin", "Test")
                try:
                    for method in ("second", "third"):
                        result = remote_client.broker_call(str(broker.path), "origin", {"method": method, "workspace": "Test"})
                        self.assertEqual(result, {"method": method})
                    with self.assertRaises(OSError):
                        remote_client.broker_call(str(broker.path), "other", {"method": "fourth", "workspace": "Test"})
                    with self.assertRaises(OSError):
                        remote_client.broker_call(str(broker.path), "origin", {"method": "fourth", "workspace": "Other"})
                finally:
                    broker.close()
                self.assertFalse(broker.path.exists())
            factory.assert_called_once()
            self.assertEqual(channel.request.call_count, 3)
            channel.close.assert_called_once()

    def test_unknown_outcome_is_not_replayed(self):
        channel = Mock()
        channel.request.side_effect = OSError("connection lost after request")
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}, clear=True), \
             patch("kzw.remote_client.Channel", return_value=channel):
            with remote_client.persistent_rpc():
                broker = remote_client.ConnectionBroker("origin", "Test")
                try:
                    with self.assertRaisesRegex(OSError, "connection lost"):
                        remote_client.broker_call(str(broker.path), "origin", {"method": "pane.reserve", "workspace": "Test"})
                finally:
                    broker.close()
            channel.request.assert_called_once()

    def test_explicit_rejection_preserves_channel_and_error_code(self):
        channel = Mock()
        channel.request.side_effect = [remote_client.RemoteRejected("conflict", "conflict"), {"exists": True}]
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}, clear=True), \
             patch("kzw.remote_client.Channel", return_value=channel) as factory:
            with remote_client.persistent_rpc():
                broker = remote_client.ConnectionBroker("origin", "Test")
                try:
                    with self.assertRaises(remote_client.RemoteRejected) as rejected:
                        remote_client.broker_call(str(broker.path), "origin", {"method": "pane.reserve", "workspace": "Test"})
                    self.assertEqual(rejected.exception.code, "conflict")
                    self.assertEqual(str(rejected.exception), "remote coordinator: conflict")
                    self.assertTrue(remote_client.broker_call(str(broker.path), "origin", {"method": "workspace.get", "workspace": "Test"})["exists"])
                finally:
                    broker.close()
            factory.assert_called_once()

    def test_matching_workspace_uses_broker_without_fallback(self):
        env = {"KZW_RPC_SOCKET": "/missing/socket", "KZW_WORKSPACE": "Test", "KZW_WORKSPACE_ORIGIN": "origin"}
        with patch.dict(os.environ, env, clear=True), patch("kzw.remote_client.direct_call") as direct:
            with self.assertRaises(OSError):
                remote_client.call("origin", {"method": "pane.reserve", "workspace": "Test"})
            direct.assert_not_called()
            remote_client.call("origin", {"method": "workspace.get", "workspace": "Other"})
            direct.assert_called_once()

    def test_separate_process_reuses_supervisor_channel(self):
        channel = Mock()
        channel.request.return_value = {"exists": True}
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}, clear=True), \
             patch("kzw.remote_client.Channel", return_value=channel) as factory:
            with remote_client.persistent_rpc():
                remote_client.direct_call("origin", {"method": "workspace.get", "workspace": "Test"})
                broker = remote_client.ConnectionBroker("origin", "Test")
                try:
                    env = os.environ.copy()
                    env.update(KZW_RPC_SOCKET=str(broker.path), KZW_WORKSPACE="Test", KZW_WORKSPACE_ORIGIN="origin")
                    code = ("from kzw.remote_client import call; "
                            "assert call('origin', {'method':'workspace.get','workspace':'Test'})['exists']")
                    for _ in range(2):
                        subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=10)
                finally:
                    broker.close()
            factory.assert_called_once()
            self.assertEqual(channel.request.call_count, 3)
