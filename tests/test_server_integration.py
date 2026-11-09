"""Opt-in SSH server test: no Kitty windows and no zmx sessions created."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from kzw import remote_client
from kzw import cli
from kzw.cli import zmx_command


@unittest.skipUnless(os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "remote server test is opt-in")
class ServerIntegration(unittest.TestCase):
    def test_server_survives_ssh_disconnect(self):
        host = os.environ["KZW_INTEGRATION_HOST"]
        remote = subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                                 'mkdir -p "$HOME/.cache" && mktemp -d "$HOME/.cache/kzw-server-it-XXXXXXXX"'],
                                check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        if not remote.startswith("/") or "/.cache/kzw-server-it-" not in remote:
            self.fail("unexpected private remote test directory")
        with patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": remote}):
            try:
                with tempfile.TemporaryDirectory() as directory:
                    remote_client.create(Path(directory), "Test", host)
                    with tempfile.TemporaryDirectory() as local_state, \
                         patch.dict(os.environ, {"XDG_STATE_HOME": local_state}), \
                         patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                         patch("kzw.cli.run", return_value=0) as run:
                        self.assertEqual(cli.attach_remote("Test", host), 0)
                        self.assertTrue(run.call_args.kwargs["remote_attached"])
                    token = remote_client.acquire_lease(Path(directory), "Test", host)
                    with tempfile.TemporaryDirectory() as second_client:
                        with self.assertRaises(ValueError):
                            remote_client.acquire_lease(Path(second_client), "Test", host)
                    before = remote_client.call(host, {"method": "server.info"})
                    first = remote_client.call(host, {"method": "workspace.get", "workspace": "Test"})
                    after = remote_client.call(host, {"method": "server.info"})
                    second = remote_client.call(host, {"method": "workspace.status", "workspace": "Test"})
                    self.assertTrue(first["exists"])
                    self.assertEqual(before, after)
                    self.assertEqual(second["revision"], first["revision"])
                    self.assertEqual(second["live"], [])
                    self.assertEqual(second["missing"], [])
                    bootstrap = cli.prepare_bootstrap(Path(directory), "Test", host, "/bin/kzw-pane")
                    journal = json.loads((Path(directory) / "pending-create.json").read_text())
                    pane, operation = journal["pane"], journal["request_id"]
                    reserved = remote_client.reserve_pane(Path(directory), pane, operation)
                    self.assertEqual(remote_client.reserve_pane(Path(directory), pane, operation), reserved)
                    self.assertEqual(remote_client.call(host, {"method": "workspace.get", "workspace": "Test"})["reservations"], [pane])
                    cli.mark_bootstrap_launching(Path(directory))
                    command = ('PATH="$HOME/.local/bin:$PATH"; export PATH; '
                               'unset ZMX_SESSION ZMX_SESSION_PREFIX; exec zmx attach ' + pane + ' /bin/sleep 60')
                    attachment = subprocess.Popen(["ssh", "-tt", host, command], stdin=subprocess.PIPE,
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        deadline = time.monotonic() + 20
                        while pane not in zmx_command(host, "list", "--short").splitlines():
                            if attachment.poll() is not None or time.monotonic() >= deadline:
                                self.fail("disposable remote zmx session did not start")
                            time.sleep(0.1)
                        self.assertEqual(cli.recover_bootstrap(Path(directory), "Test", host, "/bin/kzw-pane"), bootstrap)
                        self.assertIn("--existing", bootstrap.read_text())
                        remote_client.call(host, {"method": "workspace.put", "workspace": "Test", "request_id": "own-pane",
                                                  "revision": first["revision"], "snapshot": "test layout", "panes": [pane],
                                                  "lease_token": token})
                        self.assertEqual(remote_client.call(host, {"method": "workspace.get", "workspace": "Test"})["reservations"], [])
                        live = remote_client.call(host, {"method": "workspace.status", "workspace": "Test"})
                        self.assertEqual(live["live"], [pane])
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            changed = pool.submit(remote_client.call, host, {"method": "workspace.watch", "workspace": "Test",
                                                                            "known": live, "timeout": 10})
                            zmx_command(host, "kill", pane)
                            update = changed.result(timeout=15)
                            self.assertEqual(update["missing"], [pane])
                            self.assertEqual(update["confirmed_missing"], [pane])
                        # A fresh SSH bridge receives the persisted observation.
                        restored = remote_client.call(host, {"method": "workspace.status", "workspace": "Test"})
                        self.assertEqual(restored["confirmed_missing"], [pane])
                    finally:
                        try:
                            if pane in zmx_command(host, "list", "--short").splitlines():
                                zmx_command(host, "kill", pane)
                        except (OSError, subprocess.SubprocessError) as exc:
                            print(f"test zmx session may remain: {pane}: {exc}")
                        if attachment.poll() is None:
                            attachment.terminate()
                        attachment.wait(timeout=10)
                        attachment.stdin.close()
                    remote_client.release_lease(Path(directory))
            finally:
                stopped = False
                for _ in range(30):
                    try:
                        remote_client.call(host, {"method": "server.shutdown"})
                        stopped = True
                        break
                    except (OSError, ValueError, subprocess.SubprocessError):
                        time.sleep(0.1)
                if stopped:
                    script = ("import os,sys,time; p=sys.argv[1]; d=os.path.join(p,'kzw'); "
                              "[(time.sleep(.1)) for _ in range(30) if os.path.exists(os.path.join(d,'server.sock'))]; "
                              "[(os.unlink(os.path.join(d,n)) if os.path.isfile(os.path.join(d,n)) else None) "
                              "for n in ('remote.sqlite3','remote.sqlite3-journal','remote.sqlite3-wal',"
                              "'remote.sqlite3-shm','server.lock')]; os.rmdir(d); os.rmdir(p)")
                    result = subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                                             "python3 -c " + shlex.quote(script) + " " + shlex.quote(remote)],
                                            capture_output=True, text=True, timeout=20)
                    if result.returncode:
                        self.fail("private test state remains at " + remote + ": " + result.stderr)
                else:
                    self.fail("private test server and state remain at " + remote)
