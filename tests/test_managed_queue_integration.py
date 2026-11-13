"""Opt-in real-zmx queue ownership and reattachment, without Kitty or real Condor."""

import os
from pathlib import Path
import pty
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from kzw.remote import Store
from kzw.sessions import snapshot_tabs
from kzw.utility import queue_session_command
from workspace_fixtures import layout


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real zmx integration is opt-in")
class ManagedQueueIntegration(unittest.TestCase):
    def test_queue_has_owned_selector_entry_and_survives_client_detach(self):
        if not shutil.which("zmx") or not shutil.which("bash"):
            self.skipTest("zmx and bash are required")
        with tempfile.TemporaryDirectory(prefix="kzq-", dir=Path.home() / ".cache") as tmp:
            root = Path(tmp)
            home = root / "home"
            tools, fzf = home / ".local/bin", home / ".fzf/bin/fzf"
            picker = home / "dotfiles/scripts/tmux_condor_queue.sh"
            tools.mkdir(parents=True)
            fzf.parent.mkdir(parents=True)
            picker.parent.mkdir(parents=True)
            for tool in ("condor_q", "less", "date"):
                stub = tools / tool
                stub.write_text("#!/bin/sh\nexit 99\n")  # Bootstrap checks only; no actual Condor request.
                stub.chmod(0o700)
            fzf.write_text('#!/bin/sh\nprintf "QUEUE_READY\\n"\nIFS= read -r _\n')
            fzf.chmod(0o700)
            picker.write_text('printf "%s\\n" "$$" > "$HOME/picker.pid"\nexec fzf\n')
            (home / ".bash_profile").write_text('export PATH="$HOME/.local/bin:$PATH"\n')
            env = os.environ.copy()
            env.update(HOME=str(home), ZMX_DIR=str(root / "zmx"), SHELL="/bin/sh")
            env.pop("ZMX_SESSION", None)
            env.pop("ZMX_SESSION_PREFIX", None)
            session = "kzw-" + uuid.uuid4().hex
            clients, fds = [], []
            store = Store(root / "state.sqlite")

            def listing():
                return set(subprocess.run(["zmx", "list", "--short"], env=env, capture_output=True,
                                          text=True, check=True, timeout=5).stdout.splitlines())

            def wait_for(predicate):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.1)
                self.fail("disposable managed queue condition timed out")

            def attach(command):
                master, slave = pty.openpty()
                fds.extend([master, slave])
                proc = subprocess.Popen(["zmx", "attach", session, *command], env=env, stdin=slave,
                                        stdout=slave, stderr=slave, start_new_session=True)
                clients.append(proc)
                return proc, master

            try:
                first, _ = attach(queue_session_command())
                wait_for(lambda: (home / "picker.pid").exists() and session in listing())
                identity = (home / "picker.pid").read_text()
                with patch.dict(os.environ, env, clear=True):
                    store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 0,
                                    "request_id": "managed-queue", "snapshot": layout(session), "panes": [session]})
                    state = store.dispatch({"method": "workspace.sync", "workspace": "Test"})
                    self.assertEqual(snapshot_tabs(state["snapshot"], state["panes"]), [[session]])
                    entry = next(row for row in store.inventory()["sessions"] if row["id"] == session)
                    self.assertEqual(entry["workspace"], "Test")
                first.terminate()
                first.wait(timeout=5)
                self.assertIn(session, listing())
                second, master = attach(["/bin/false"])
                time.sleep(0.2)
                self.assertIsNone(second.poll(), "reattachment ran a replacement command")
                self.assertEqual((home / "picker.pid").read_text(), identity)
                self.assertEqual(listing(), {session})
                os.write(master, b"\r")
                wait_for(lambda: session not in listing())
                with patch.dict(os.environ, env, clear=True):
                    # Absence alone is not authorization to discard the saved entry.
                    self.assertEqual(store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [session])
            finally:
                for client in clients:
                    if client.poll() is None:
                        client.terminate()
                        client.wait(timeout=5)
                try:
                    if session in listing():
                        subprocess.run(["zmx", "kill", session], env=env, capture_output=True, text=True,
                                       check=True, timeout=5)
                    self.assertNotIn(session, listing(), "disposable queue session cleanup failed: " + str(root))
                finally:
                    store.close()
                    for fd in fds:
                        os.close(fd)
