import base64
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kzw import bridge, remote_client


class BridgeTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        env = patch.dict(os.environ, {"HOME": home.name, "XDG_CONFIG_HOME": str(self.home / "config"),
                                      "KZW_REMOTE_STATE_HOME": ""})
        env.start()
        self.addCleanup(env.stop)
        self.run_process = subprocess.run

    def install_locally(self, command, **kwargs):
        self.assertEqual(command[-2], "origin")
        return self.run_process(["sh", "-c", command[-1]], **kwargs)

    def test_install_and_enable_only_selected_host(self):
        self.assertIn("python3 -u -c", remote_client.ssh_command("origin")[-1])
        with patch("kzw.bridge.subprocess.run", side_effect=self.install_locally):
            bridge.install("origin", enable=False)
            self.assertEqual(bridge.installed_hosts(), set())
            bridge.install("origin")
        self.assertEqual(bridge.installed_hosts(), {"origin"})
        self.assertIn("python3 -u -c", remote_client.ssh_command("other")[-1])
        command = remote_client.ssh_command("origin")[-1]
        self.assertLess(len(command), 500)
        self.assertTrue(command.endswith(hashlib.sha256(bridge.encoded_source()).hexdigest()))
        self.assertFalse(list((self.home / ".local/bin").glob(".kzw-bridge.*")))

    def test_staged_install_preserves_the_previous_launcher(self):
        path = self.home / ".local/bin/kzw-bridge"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"previous")
        with patch("kzw.bridge.subprocess.run", side_effect=self.install_locally):
            bridge.install("origin", stage=True)
        build = hashlib.sha256(bridge.encoded_source()).hexdigest()
        self.assertEqual(path.read_bytes(), b"previous")
        staged = path.with_name(path.name + "-" + build)
        self.assertEqual(self.run_process([str(staged), "--build"], capture_output=True, text=True).stdout.strip(), build)

    def test_failed_validation_preserves_existing_executable_and_config(self):
        with patch("kzw.bridge.subprocess.run", side_effect=self.install_locally):
            bridge.install("origin", enable=False)
            path = self.home / ".local/bin/kzw-bridge"
            previous = path.read_bytes()
            with patch("kzw.bridge.executable", return_value=b"#!/bin/sh\necho wrong\n"):
                with self.assertRaises(subprocess.CalledProcessError):
                    bridge.install("origin")
        self.assertEqual(path.read_bytes(), previous)
        self.assertFalse(bridge.host_file().exists())
        self.assertFalse(list(path.parent.glob(".kzw-bridge.*")))

    def test_missing_and_mismatched_launcher_report_errors_without_fallback(self):
        with patch("kzw.bridge.installed_hosts", return_value={"origin"}):
            command = remote_client.ssh_command("origin")[-1]
        for installed, message in ((False, "missing or not executable"), (True, "version differs")):
            with self.subTest(installed=installed):
                if installed:
                    path = self.home / ".local/bin/kzw-bridge"
                    path.parent.mkdir(parents=True)
                    path.write_bytes(bridge.executable(base64.b64encode(b"pass\n")))
                    path.chmod(0o700)
                channel = remote_client.Channel(["sh", "-c", command])
                try:
                    with self.assertRaisesRegex(OSError, message):
                        channel.request({"method": "workspace.put"})
                finally:
                    channel.close()

    def test_remote_state_override_is_quoted_in_both_modes(self):
        value = "/state with ' quotes; $HOME"
        with patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": value}):
            for hosts in (set(), {"origin"}):
                with patch("kzw.bridge.installed_hosts", return_value=hosts):
                    command = remote_client.ssh_command("origin")[-1]
                # Parse the generated export independently of the RPC program.
                suffix = 'bridge="$HOME/' if hosts else "exec python3"
                prefix = command[:command.index(suffix)]
                result = self.run_process(["sh", "-c", prefix + 'printf "%s" "$XDG_STATE_HOME"'],
                                          capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, value)

    def test_persistent_transport_does_not_reconnect_to_replay_failed_request(self):
        log = self.home / "request"
        command = ["sh", "-c", 'IFS= read -r line; printf "%s\\n" "$line" >> "$1"', "sh", str(log)]
        request = {"method": "workspace.put"}
        with remote_client.persistent_rpc(), \
                patch("kzw.network.require"), \
                patch("kzw.remote_client.coordinator_command", return_value=command) as start:
            with self.assertRaisesRegex(OSError, "outcome may be unknown"):
                remote_client.direct_call("origin", request)
        start.assert_called_once_with("origin")
        self.assertEqual(log.read_text(), '{"method":"workspace.put"}\n')
