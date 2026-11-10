import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from kzw import remote_client


class BrokerTests(unittest.TestCase):
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
