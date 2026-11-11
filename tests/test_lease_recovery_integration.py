"""Opt-in real SSH outage/expiry test, isolated Test state and one owned zmx session."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from kzw import remote_client
from kzw.cli import zmx_command
from workspace_fixtures import layout


@unittest.skipUnless(os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "remote lease recovery test is opt-in")
class LeaseRecoveryIntegration(unittest.TestCase):
    def test_outage_past_ttl_and_lost_recovery_ack_preserve_session_and_pending_layout(self):
        host = os.environ["KZW_INTEGRATION_HOST"]
        root = subprocess.check_output(
            ["ssh", "-o", "BatchMode=yes", host,
             'mkdir -p "$HOME/.cache" && mktemp -d "$HOME/.cache/kzw-lease-it-XXXXXXXX"'],
            text=True, timeout=20).strip()
        self.assertTrue(root.startswith("/") and "/.cache/kzw-lease-it-" in root)
        pane = "kzw-" + uuid.uuid4().hex
        attachment = None
        stop = lambda: None
        with patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": root, "KZW_RPC_SOCKET": ""}), \
             tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            try:
                remote_client.create(directory, "Test", host)
                original = remote_client.acquire_lease(directory, "Test", host)
                remote_client.reserve_pane(directory, pane, uuid.uuid4().hex)
                snapshot, panes = remote_client.translate(layout(pane), "/bin/kzw-pane", host, False)
                remote_client.stage_layout(directory, snapshot, panes)
                command = ('PATH="$HOME/.local/bin:$PATH"; export PATH; unset ZMX_SESSION ZMX_SESSION_PREFIX; '
                           'exec zmx attach ' + pane + ' /bin/sleep 180')
                attachment = subprocess.Popen(["ssh", "-tt", "-o", "BatchMode=yes", host, command],
                                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 15
                while pane not in zmx_command(host, "list", "--short").splitlines():
                    if attachment.poll() is not None or time.monotonic() >= deadline:
                        self.fail("disposable zmx session did not start")
                    time.sleep(0.1)
                remote_client.publish(directory, snapshot, panes)

                def session_identity():
                    line = next(line for line in zmx_command(host, "list").splitlines() if "name=" + pane + "\t" in line)
                    fields = dict(field.split("=", 1) for field in line.strip().split("\t"))
                    return fields["pid"], fields["created"]

                session_before = session_identity()
                server_before = remote_client.call(host, {"method": "workspace.get", "workspace": "Test"})
                offline, renewed, recovered = threading.Event(), threading.Event(), threading.Event()
                real_channel = remote_client.Channel
                dropped = []

                class InterruptedChannel(real_channel):
                    def request(channel, request, timeout=30):
                        heartbeat = threading.current_thread().name == "kzw-lease"
                        if heartbeat and offline.is_set():
                            raise OSError("simulated coordinator outage")
                        result = super().request(request, timeout)
                        if heartbeat and request["method"] == "workspace.lease.recover" and not dropped:
                            dropped.append(request.copy())
                            raise OSError("simulated lost recovery acknowledgement")
                        if heartbeat and request["method"] == "workspace.lease.renew":
                            renewed.set()
                            if request["token"] != original:
                                recovered.set()
                        return result

                with patch("kzw.remote_client.Channel", InterruptedChannel):
                    stop = remote_client.start_lease_heartbeat(directory)
                    self.assertTrue(renewed.wait(15))
                    offline.set()
                    # Exercise actual server wall-clock expiry, not a database edit.
                    time.sleep(32)
                    changed = snapshot.replace("new_tab Test", "new_tab Recovered")
                    with self.assertRaises(remote_client.RemoteRejected):
                        remote_client.publish(directory, changed, panes)
                    pending = json.loads((directory / "remote.json").read_text())["pending"]
                    offline.clear()
                    self.assertTrue(recovered.wait(25), "automatic lease recovery did not finish")
                    self.assertEqual(json.loads((directory / "remote.json").read_text())["pending"], pending)
                    self.assertNotEqual(remote_client.lease_token(directory), original)
                    remote_client.publish(directory, changed, panes)
                    stop()
                    stop = lambda: None
                after = remote_client.call(host, {"method": "workspace.get", "workspace": "Test"})
                self.assertEqual(after["revision"], server_before["revision"] + 1)
                self.assertEqual(after["panes"], [pane])
                self.assertFalse(json.loads((directory / "remote.json").read_text()).get("pending"))
                self.assertEqual(session_identity(), session_before)
                self.assertIsNone(attachment.poll())
                with self.assertRaises(remote_client.RemoteRejected):
                    remote_client.call(host, {"method": "workspace.lease.renew", "workspace": "Test", "token": original})
            finally:
                stop()
                if attachment is not None:
                    if pane in zmx_command(host, "list", "--short").splitlines():
                        zmx_command(host, "kill", pane)
                    if attachment.poll() is None:
                        attachment.terminate()
                    attachment.wait(timeout=10)
                    attachment.stdin.close()
                remote_client.release_lease(directory)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        remote_client.call(host, {"method": "server.shutdown"})
                        break
                    except (OSError, ValueError, subprocess.SubprocessError):
                        if time.monotonic() >= deadline:
                            self.fail("isolated test server remains at " + root)
                        time.sleep(0.1)
                cleanup = (
                    "import os,sys,time,shutil; p=sys.argv[1]; "
                    "assert os.path.dirname(p)==os.path.expanduser('~/.cache') and os.path.basename(p).startswith('kzw-lease-it-'); "
                    "s=os.path.join(p,'kzw','server.sock'); "
                    "[(time.sleep(.1)) for _ in range(30) if os.path.exists(s)]; "
                    "assert not os.path.exists(s), 'test server still running'; shutil.rmtree(p)")
                subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                                "python3 -c " + shlex.quote(cleanup) + " " + shlex.quote(root)], check=True, timeout=20)
