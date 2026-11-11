"""Opt-in zmx attachment semantics with an isolated socket directory."""

import os
from pathlib import Path
import pty
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from kzw.remote import Store
from workspace_fixtures import layout


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real integration is opt-in")
class SessionAttachIntegration(unittest.TestCase):
    def test_existing_session_ignores_fail_fast_creation_command(self):
        if not shutil.which("zmx"):
            self.skipTest("zmx is required")
        with tempfile.TemporaryDirectory(prefix="kzs-", dir=Path.home() / ".cache") as directory:
            env = os.environ.copy()
            env.update(ZMX_DIR=str(Path(directory) / "zmx"), SHELL="/bin/bash")
            env.pop("ZMX_SESSION", None)
            env.pop("ZMX_SESSION_PREFIX", None)
            pane = "kzw-" + "e" * 32
            clients = []
            fds = []
            store = Store(Path(directory) / "server.sqlite")
            registered = False
            try:
                def attach(*extra):
                    master, slave = pty.openpty()
                    fds.extend([master, slave])
                    proc = subprocess.Popen(["zmx", "attach", pane, *extra], env=env,
                                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                    clients.append(proc)
                    return proc

                attach()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    listing = subprocess.run(["zmx", "list", "--short"], env=env, capture_output=True,
                                             text=True, timeout=5, check=True).stdout.splitlines()
                    if pane in listing:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("disposable zmx session did not start")
                second = attach("/bin/false")
                time.sleep(1)
                self.assertIsNone(second.poll(), "existing session did not remain attached")
                listing = subprocess.run(["zmx", "list", "--short"], env=env, capture_output=True,
                                         text=True, timeout=5, check=True).stdout.splitlines()
                self.assertIn(pane, listing)
                # Inventory uses the origin's full namespace, even when queried
                # from inside a prefixed/nested zmx session.
                with patch.dict(os.environ, {**env, "ZMX_SESSION": "nested", "ZMX_SESSION_PREFIX": "nested-"}):
                    inventory = store.inventory()
                    self.assertEqual(inventory["observation"]["sessions"], [pane])
                    self.assertEqual(inventory["sessions"][0]["assignment"], "unassigned")
                    store.dispatch({"method": "workspace.put", "workspace": "Test", "request_id": "register",
                                    "revision": 0, "snapshot": layout(pane), "panes": [pane]})
                    registered = True
                    self.assertEqual(store.inventory()["sessions"][0]["workspace"], "Test")
            finally:
                for client in clients:
                    if client.poll() is None:
                        client.terminate()
                        client.wait(timeout=5)
                listing = subprocess.run(["zmx", "list", "--short"], env=env, capture_output=True,
                                         text=True, timeout=5, check=True).stdout.splitlines()
                if pane in listing:
                    subprocess.run(["zmx", "kill", pane], env=env, capture_output=True,
                                    text=True, timeout=5, check=True)
                try:
                    if registered:
                        with patch.dict(os.environ, env):
                            store.observe_detached()
                            inventory = store.inventory()
                        self.assertEqual(inventory["sessions"][0]["presence"], "missing")
                        self.assertEqual(inventory["workspaces"]["Test"]["snapshot"], layout(pane))
                finally:
                    store.close()
                for fd in fds:
                    os.close(fd)
