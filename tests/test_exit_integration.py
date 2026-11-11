"""Opt-in origin-side exit receipts with real SSH/zmx and isolated Test state."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from kzw import remote_client
from kzw.cli import zmx_command
from workspace_fixtures import layout


@unittest.skipUnless(os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "remote exits test is opt-in")
class RemoteExitIntegration(unittest.TestCase):
    def test_disconnect_is_retained_but_shell_exit_is_committed(self):
        host = os.environ["KZW_INTEGRATION_HOST"]
        root = subprocess.check_output(
            ["ssh", "-o", "BatchMode=yes", host,
             'mkdir -p "$HOME/.cache" && mktemp -d "$HOME/.cache/kzw-exit-it-XXXXXXXX"'],
            text=True, timeout=20).strip()
        self.assertTrue(root.startswith("/") and "/.cache/kzw-exit-it-" in root)
        pane = "kzw-" + uuid.uuid4().hex
        children = []
        stop = lambda: None
        with patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": root, "KZW_RPC_SOCKET": ""}), tempfile.TemporaryDirectory() as local:
            directory = Path(local)
            try:
                remote_client.create(directory, "Test", host)
                remote_client.acquire_lease(directory, "Test", host)
                stop = remote_client.start_lease_heartbeat(directory)
                remote_client.reserve_pane(directory, pane, uuid.uuid4().hex)
                snapshot, panes = remote_client.translate(layout(pane), "/bin/kzw-pane", host, False)
                remote_client.stage_layout(directory, snapshot, panes)

                def attach(existing=False):
                    command = ('PATH="$HOME/.local/bin:$PATH"; export PATH; unset ZMX_SESSION ZMX_SESSION_PREFIX; ' +
                               ('zmx list --short | grep -Fx ' + pane + ' >/dev/null || exit 1; ' if existing else '') +
                               'exec zmx attach ' + pane + ('' if existing else ' /bin/sh'))
                    child = subprocess.Popen(["ssh", "-tt", "-o", "BatchMode=yes", host, command],
                                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    children.append(child)
                    return child

                def wait_for(predicate):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if predicate():
                            return
                        time.sleep(0.1)
                    self.fail("remote Test exit condition timed out")

                child = attach()
                wait_for(lambda: pane in zmx_command(host, "list", "--short").splitlines())
                remote_client.publish(directory, snapshot, panes)
                listing = zmx_command(host, "list")
                identity = next(line for line in listing.splitlines() if "name=" + pane + "\t" in line)
                fields = dict(field.split("=", 1) for field in identity.strip().split("\t"))
                child.terminate()
                child.wait(timeout=10)
                self.assertEqual(remote_client.exit_receipts(directory, [pane]), {})
                current = next(line for line in zmx_command(host, "list").splitlines() if "name=" + pane + "\t" in line)
                after = dict(field.split("=", 1) for field in current.strip().split("\t"))
                self.assertEqual((fields["pid"], fields["created"]), (after["pid"], after["created"]))
                resumed = attach(existing=True)
                # A complete prompt is unnecessary; a shell reads queued input.
                resumed.stdin.write(b"exit 7\n")
                resumed.stdin.flush()
                wait_for(lambda: bool(remote_client.exit_receipts(directory, [pane])))
                receipt = remote_client.exit_receipts(directory, [pane])
                self.assertEqual(receipt[pane]["shell_pid"], int(fields["pid"]))
                remote_client.publish(directory, "# kzw-empty-workspace\n", [], exit_receipts=receipt)
                remote = remote_client.call(host, {"method": "workspace.get", "workspace": "Test"})
                self.assertEqual((remote["panes"], remote["snapshot"]), ([], "# kzw-empty-workspace\n"))
                self.assertEqual(remote_client.call(host, {"method": "cleanup.list", "workspace": "Test"})["panes"], [])
                self.assertNotIn(pane, zmx_command(host, "list", "--short").splitlines())
            finally:
                stop()
                if pane in zmx_command(host, "list", "--short").splitlines():
                    zmx_command(host, "kill", pane)
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=10)
                    child.stdin.close()
                remote_client.release_lease(directory)
                remote_client.call(host, {"method": "server.shutdown"})
                cleanup = (
                    "import os,sys,time,shutil; p=sys.argv[1]; "
                    "assert os.path.dirname(p)==os.path.expanduser('~/.cache') and os.path.basename(p).startswith('kzw-exit-it-'); "
                    "s=os.path.join(p,'kzw','server.sock'); "
                    "[(time.sleep(.1)) for _ in range(50) if os.path.exists(s)]; "
                    "assert not os.path.exists(s), 'test server still running'; shutil.rmtree(p)")
                subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                                "python3 -c " + shlex.quote(cleanup) + " " + shlex.quote(root)], check=True, timeout=20)
