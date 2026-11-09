"""Opt-in remote-origin smoke test with isolated local and remote state."""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from kzw.cli import state_dir, zmx_command
from kzw import remote_client
from unittest.mock import patch
from kzw.core import pane_map


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real remote integration is opt-in")
class RemoteOriginIntegration(unittest.TestCase):
    def setUp(self):
        self.host = os.environ.get("KZW_INTEGRATION_HOST")
        if not self.host or not shutil.which("kitty") or not shutil.which("kzw") or not shutil.which("kzw-pane"):
            self.skipTest("requires KZW_INTEGRATION_HOST, kitty, kzw and kzw-pane on PATH")
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.local = tempfile.TemporaryDirectory(prefix="kr-", dir=cache)
        self.remote = subprocess.run(["ssh", "-o", "BatchMode=yes", self.host,
                                      'mkdir -p "$HOME/.cache" && mktemp -d "$HOME/.cache/kzw-it-XXXXXXXX"'],
                                     capture_output=True, text=True, check=True, timeout=20).stdout.strip()
        if not self.remote.startswith("/") or "/.cache/kzw-it-" not in self.remote:
            raise ValueError("unexpected remote test state path")
        self.owned = set()
        self.process = None
        self.state = None
        self.log = Path(self.local.name) / "supervisor.log"
        self.addCleanup(self.cleanup)

    def env(self, client):
        env = os.environ.copy()
        env["XDG_STATE_HOME"] = str(Path(self.local.name) / client)
        env["KZW_REMOTE_STATE_HOME"] = self.remote
        if sys.platform == "darwin":
            env["KZW_TEST_KITTY_BACKGROUND"] = "1"
        if getattr(self, "ssh_shim", None):
            env["PATH"] = str(self.ssh_shim) + os.pathsep + env.get("PATH", "")
        return env

    def kzw(self, client, *args):
        confirmation = "y\n" if args[0] in ("close-pane", "close-tab") else None
        result = subprocess.run(["kzw", *args], env=self.env(client), capture_output=True,
                                input=confirmation, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(f"kzw {args} failed: {result.stderr}")
        return result.stdout

    def kitty(self, *args):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.local.name) / self.state)}):
            socket = state_dir("Test", self.host) / "kitty.sock"
        return subprocess.run(["kitty", "@", "--to", f"unix:{socket}", *args],
                              capture_output=True, text=True, check=True, timeout=10).stdout

    def tree(self):
        tree = json.loads(self.kitty("ls"))
        self.owned.update(pane for pane, _ in pane_map(tree).values())
        return tree

    def attachment_pid(self, window, pane):
        """Find only the SSH client descended from our isolated Kitty pane."""
        output = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], text=True,
                                capture_output=True, check=True).stdout
        processes = {}
        for line in output.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                processes[int(fields[0])] = (int(fields[1]), fields[2])
        descendants = {window["pid"]}
        while True:
            expanded = descendants | {pid for pid, (parent, _) in processes.items() if parent in descendants}
            if expanded == descendants:
                break
            descendants = expanded
        matches = [pid for pid, (_, command) in processes.items() if pid in descendants and
                   Path(command.split()[0]).name == "ssh" and pane in command]
        return matches[0] if len(matches) == 1 else None

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + 45
        last = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                last = exc
            time.sleep(0.1)
        log = self.log.read_text() if self.log.exists() else ""
        self.fail(f"{message}: {last}\n{log}")

    def attach(self, client, count, command="attach"):
        self.state = client
        with self.log.open("a") as log:
            self.process = subprocess.Popen(["kzw", command, "Test", "--host", self.host],
                                            env=self.env(client), stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.local.name) / client)}):
            directory = state_dir("Test", self.host)
        def ready():
            panes = pane_map(self.tree())
            manifest = directory / "panes.json"
            return (len(panes) == count and not (directory / "pending-create.json").exists() and
                    manifest.is_file() and set(json.loads(manifest.read_text())) == {value[0] for value in panes.values()})
        self.wait_for(ready, "remote workspace did not attach and finish registration")

    def detach(self):
        self.kzw(self.state, "detach", "Test", "--host", self.host)
        self.process.wait(timeout=20)
        self.process = None

    def cleanup(self):
        if self.state:
            try:
                with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.local.name) / self.state)}):
                    socket = state_dir("Test", self.host) / "kitty.sock"
                if socket.exists():
                    try:
                        self.tree()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        print("kzw remote integration: could not enumerate all test pane IDs; inspect for leftover sessions")
                    self.kitty("close-window", "--match", "all")
            except (OSError, ValueError, subprocess.SubprocessError):
                print("kzw remote integration: test Kitty may still be open")
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print("kzw remote integration: test supervisor may still be running")
        for pane in self.owned:
            try:
                if pane in zmx_command(self.host, "list", "--short").splitlines():
                    zmx_command(self.host, "kill", pane)
            except (OSError, subprocess.SubprocessError):
                print(f"kzw remote integration: manual cleanup may be needed for {pane}")
        stopped = False
        with patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": self.remote}):
            for _ in range(30):
                try:
                    remote_client.call(self.host, {"method": "server.shutdown"})
                    stopped = True
                    break
                except (OSError, ValueError, subprocess.SubprocessError):
                    time.sleep(0.1)
        if not stopped:
            print(f"kzw remote integration: test server and state remain at {self.remote}")
            self.local.cleanup()
            return
        # Remove only the private SQLite files and directories created by this
        # test; never recursively remove or touch any other remote path.
        script = ("import os,sys,time; p=sys.argv[1]; d=os.path.join(p,'kzw'); "
                  "[(time.sleep(.1)) for _ in range(30) if os.path.exists(os.path.join(d,'server.sock'))]; "
                  "[(os.unlink(os.path.join(d,n)) if os.path.isfile(os.path.join(d,n)) else None) "
                  "for n in ('remote.sqlite3','remote.sqlite3-journal','remote.sqlite3-wal','remote.sqlite3-shm','server.lock')]; "
                  "os.rmdir(d); os.rmdir(p)")
        try:
            subprocess.run(["ssh", "-o", "BatchMode=yes", self.host,
                            "python3 -c " + shlex.quote(script) + " " + shlex.quote(self.remote)],
                           capture_output=True, text=True, check=True, timeout=20)
        except subprocess.SubprocessError:
            print(f"kzw remote integration: test state remains at {self.remote}")
        self.local.cleanup()

    def test_create_and_attach_from_fresh_local_state(self):
        local_workspace = Path(self.local.name) / "a" / "kzw" / "Test"
        local_workspace.mkdir(parents=True)
        local_snapshot = local_workspace / "workspace.kitty-session"
        local_snapshot.write_text("local workspace must remain untouched\n")
        self.kzw("a", "create", "Test", "--host", self.host)
        self.assertIn("Test", self.kzw("a", "list", "--host", self.host).splitlines())
        duplicate = subprocess.run(["kzw", "create", "Test", "--host", self.host],
                                   env=self.env("a"), capture_output=True, text=True, timeout=30)
        self.assertNotEqual(duplicate.returncode, 0)
        self.attach("a", 1)
        first = next(iter(pane_map(self.tree())))
        first_pane = pane_map(self.tree())[first][0]
        self.kitty("send-text", "--match", f"id:{first}", "echo REMOTE_WORKSPACE_TEST\\r")
        self.wait_for(lambda: "REMOTE_WORKSPACE_TEST" in self.kitty("get-text", "--match", f"id:{first}", "--extent", "all"), "output missing")
        self.kitty("launch", "--match", f"id:{first}", "--next-to", f"id:{first}", "--location=hsplit")
        self.wait_for(lambda: len(pane_map(self.tree())) == 2, "new remote pane missing")
        ids = {v[0] for v in pane_map(self.tree()).values()}
        self.wait_for(lambda: ids.issubset(set(zmx_command(self.host, "list", "--short").splitlines())), "zmx sessions missing")
        self.detach()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.local.name) / "a")}):
            self.assertFalse(list((state_dir("Test", self.host) / "native-pending").glob("*.json")))
        with patch.dict(os.environ, self.env("a")):
            history = remote_client.call(self.host, {"method": "workspace.events", "workspace": "Test", "after": 0})["events"]
            registered = remote_client.call(self.host, {"method": "workspace.get", "workspace": "Test"})
        reserved = {event["data"]["pane"] for event in history if event["kind"] == "pane.reserved"}
        self.assertTrue(ids.issubset(reserved))
        self.assertEqual(registered["reservations"], [])
        self.assertEqual(local_snapshot.read_text(), "local workspace must remain untouched\n")
        self.attach("b", 2)
        restored = pane_map(self.tree())
        self.assertEqual({v[0] for v in restored.values()}, ids)
        first_id = next(key for key, value in restored.items() if value[0] == first_pane)
        self.wait_for(lambda: "REMOTE_WORKSPACE_TEST" in self.kitty("get-text", "--match", f"id:{first_id}", "--extent", "all"), "restored output missing")
        second_id = next(key for key in restored if key != first_id)
        second_pane = restored[second_id][0]
        self.kitty("send-text", "--match", f"id:{second_id}", "echo EXIT_READY\\r")
        def second_prompt_ready():
            text = self.kitty("get-text", "--match", f"id:{second_id}", "--extent", "all")
            _, marker, tail = text.rpartition("\nEXIT_READY\n")
            return bool(marker and tail.strip())
        self.wait_for(second_prompt_ready, "second remote shell prompt was not ready")
        # Give the remote line editor time to enter its input loop after its
        # prompt is rendered; EOF sent while the previous command is still
        # finishing can otherwise be consumed before the shell reads it.
        time.sleep(0.2)
        self.kitty("send-text", "--match", f"id:{second_id}", "\\x04")
        try:
            self.wait_for(lambda: len(pane_map(self.tree())) == 1, "exited remote pane not removed")
        except AssertionError as exc:
            text = self.kitty("get-text", "--match", f"id:{second_id}", "--extent", "all")
            sessions = zmx_command(self.host, "list", "--short")
            self.fail(f"{exc}\nSecond pane text:\n{text}\nRemote sessions:\n{sessions}")
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.local.name) / "b")}):
            manifest = state_dir("Test", self.host) / "panes.json"
        self.wait_for(lambda: json.loads(manifest.read_text()) == [first_pane], "remote exit snapshot not saved")
        self.assertNotIn(second_pane, zmx_command(self.host, "list", "--short").splitlines())
        self.detach()
        self.attach("c", 1)
        current = pane_map(self.tree())
        self.assertEqual({value[0] for value in current.values()}, {first_pane})
        self.kitty("send-text", "--match", f"id:{next(iter(current))}", "\\x04")
        self.process.wait(timeout=45)
        self.process = None
        self.assertIn("has no live panes", self.kzw("d", "attach", "Test", "--host", self.host))

    def test_confirmed_close_split_then_last_pane(self):
        self.kzw("a", "create", "Test", "--host", self.host)
        self.attach("a", 1)
        first = next(iter(pane_map(self.tree())))
        self.kitty("launch", "--match", f"id:{first}", "--next-to", f"id:{first}", "--location=hsplit")
        self.wait_for(lambda: len(pane_map(self.tree())) == 2, "split missing")
        panes = pane_map(self.tree())
        second = next(key for key in panes if key != first)
        self.wait_for(lambda: {v[0] for v in panes.values()}.issubset(
            set(zmx_command(self.host, "list", "--short").splitlines())), "sessions missing")
        self.kzw("a", "close-pane", "Test", "--host", self.host, "--window-id", str(second))
        self.wait_for(lambda: len(pane_map(self.tree())) == 1, "closed pane remains")
        self.assertNotIn(panes[second][0], zmx_command(self.host, "list", "--short").splitlines())
        self.kzw("a", "close-pane", "Test", "--host", self.host, "--window-id", str(first))
        self.process.wait(timeout=30)
        self.process = None
        self.assertNotIn(panes[first][0], zmx_command(self.host, "list", "--short").splitlines())
        self.assertIn("has no live panes", self.kzw("b", "attach", "Test", "--host", self.host))

    def test_explicit_new_pane_reopens_empty_workspace(self):
        self.kzw("a", "create", "Test", "--host", self.host)
        self.attach("a", 1)
        window, (old_pane, _) = next(iter(pane_map(self.tree()).items()))
        self.kzw("a", "close-pane", "Test", "--host", self.host, "--window-id", str(window))
        self.process.wait(timeout=30)
        self.process = None
        self.assertNotIn(old_pane, zmx_command(self.host, "list", "--short").splitlines())
        self.assertIn("has no live panes", self.kzw("b", "attach", "Test", "--host", self.host))
        self.attach("b", 1, command="new-pane")
        new_pane = next(iter(pane_map(self.tree()).values()))[0]
        self.assertNotEqual(new_pane, old_pane)
        self.assertIn(new_pane, zmx_command(self.host, "list", "--short").splitlines())
        self.detach()

    def test_interrupted_bootstrap_reattaches_existing_session(self):
        from kzw.cli import mark_bootstrap_launching, prepare_bootstrap
        self.kzw("a", "create", "Test", "--host", self.host)
        with patch.dict(os.environ, self.env("a")):
            directory = state_dir("Test", self.host)
            remote_client.acquire_lease(directory, "Test", self.host)
            try:
                session = prepare_bootstrap(directory, "Test", self.host, shutil.which("kzw-pane"))
                record = json.loads((directory / "pending-create.json").read_text())
                pane = record["pane"]
                self.owned.add(pane)
                mark_bootstrap_launching(directory)
                command = ('PATH="$HOME/.local/bin:$PATH"; export PATH; unset ZMX_SESSION ZMX_SESSION_PREFIX; '
                           'exec zmx attach ' + pane + ' /bin/sleep 60')
                attachment = subprocess.Popen(["ssh", "-tt", self.host, command], stdin=subprocess.PIPE,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    self.wait_for(lambda: pane in zmx_command(self.host, "list", "--short").splitlines(),
                                  "reserved bootstrap session missing")
                finally:
                    if attachment.poll() is None:
                        attachment.terminate()
                    attachment.wait(timeout=10)
                    attachment.stdin.close()
                self.assertIn(pane, zmx_command(self.host, "list", "--short").splitlines())
            finally:
                remote_client.release_lease(directory)
        self.attach("a", 1)
        self.assertEqual({value[0] for value in pane_map(self.tree()).values()}, {pane})
        self.assertFalse(session.exists())
        self.detach()
        self.assertIn(pane, zmx_command(self.host, "list", "--short").splitlines())

    def test_fenced_pane_does_not_reconnect_under_another_lease(self):
        self.kzw("a", "create", "Test", "--host", self.host)
        self.attach("a", 1)
        tree = self.tree()
        window = tree[0]["tabs"][0]["windows"][0]
        pane = pane_map(tree)[window["id"]][0]
        ssh_pid = self.wait_for(lambda: self.attachment_pid(window, pane), "test attachment missing")
        with patch.dict(os.environ, self.env("a")):
            directory = state_dir("Test", self.host)
            token = remote_client.lease_token(directory)
            remote_client.call(self.host, {"method": "workspace.lease.release", "workspace": "Test", "token": token})
            with tempfile.TemporaryDirectory(prefix="kzw-second-") as other:
                remote_client.acquire_lease(Path(other), "Test", self.host)
                try:
                    os.kill(ssh_pid, signal.SIGKILL)
                    self.wait_for(lambda: "lease lost or held by another client" in
                                  self.kitty("get-text", "--match", f'id:{window["id"]}', "--extent", "all"),
                                  "fenced pane did not report the rejected reconnect")
                    self.assertIsNone(self.attachment_pid(window, pane))
                    self.assertEqual({value[0] for value in pane_map(self.tree()).values()}, {pane})
                    self.assertIn(pane, zmx_command(self.host, "list", "--short").splitlines())
                finally:
                    remote_client.release_lease(Path(other))

    def test_server_removes_view_when_wrapper_cannot_report_exit(self):
        self.kzw("a", "create", "Test", "--host", self.host)
        self.attach("a", 1)
        tree = self.tree()
        window = tree[0]["tabs"][0]["windows"][0]
        pane = pane_map(tree)[window["id"]][0]
        self.assertNotEqual(window.get("user_vars", {}).get("kzw_attach_ended"), "1")
        self.wait_for(lambda: pane in zmx_command(self.host, "list", "--short").splitlines(), "test session missing")
        pid = window["pid"]
        os.kill(pid, signal.SIGSTOP)
        try:
            zmx_command(self.host, "kill", pane)
            self.process.wait(timeout=30)
            self.process = None
            self.assertIn("has no live panes", self.kzw("b", "attach", "Test", "--host", self.host))
        finally:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
            if result.returncode == 0 and "kzw-pane" in result.stdout and pane in result.stdout:
                os.kill(pid, signal.SIGCONT)

    def test_kitten_confirmation_cancel_and_accept(self):
        self.kzw("a", "create", "Test", "--host", self.host)
        self.attach("a", 1)
        window, (pane, _) = next(iter(pane_map(self.tree()).items()))
        self.wait_for(lambda: pane in zmx_command(self.host, "list", "--short").splitlines(), "session missing")
        kitten = Path(__file__).parents[1] / "kzw" / "close_kitten.py"
        def prompt():
            self.kitty("action", "--match", f"id:{window}", "kitten", str(kitten), "pane")
            self.wait_for(lambda: "terminate" in self.kitty("get-text", "--match", "state:focused"), "confirmation missing")
        prompt()
        self.kitty("send-key", "--match", "state:focused", "enter")
        self.wait_for(lambda: "terminate" not in self.kitty("get-text", "--match", "state:focused"), "cancel prompt remains")
        self.wait_for(lambda: pane in zmx_command(self.host, "list", "--short").splitlines(), "cancel lost session")
        prompt()
        self.kitty("send-text", "--match", "state:focused", "y")
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired as exc:
            self.fail(f"confirmed close did not complete: {exc}\n{self.log.read_text()}\n"
                      f"focused text: {self.kitty('get-text', '--match', 'state:focused')}")
        self.process = None
        self.wait_for(lambda: pane not in zmx_command(self.host, "list", "--short").splitlines(), "confirmed session remains")

    def test_managed_creation_splits_and_tab(self):
        self.kzw("a", "create", "Test", "--host", self.host)
        self.attach("a", 1)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.local.name) / "a")}):
            directory = state_dir("Test", self.host)
        kitten = Path(__file__).parents[1] / "kzw" / "create_kitten.py"
        for kind, count in (("right", 2), ("below", 3), ("tab", 4)):
            target = next(iter(pane_map(self.tree())))
            self.kitty("action", "--match", f"id:{target}", "kitten", str(kitten), kind)
            self.wait_for(lambda: len(pane_map(self.tree())) == count and
                          not (directory / "pending-create.json").exists() and
                          len(json.loads((directory / "panes.json").read_text())) == count,
                          f"managed {kind} creation did not complete")
        created = {value[0] for value in pane_map(self.tree()).values()}
        self.assertTrue(created.issubset(set(zmx_command(self.host, "list", "--short").splitlines())))
        self.detach()
        self.attach("b", 4)
        self.assertEqual(created, {value[0] for value in pane_map(self.tree()).values()})
        self.detach()

    def test_managed_creation_recovers_after_lost_stage_acknowledgement(self):
        self.kzw("a", "create", "Test", "--host", self.host)
        self.attach("a", 1)
        first = pane_map(self.tree())
        window = next(iter(first))
        target = first[window][0]
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.local.name) / "a")}):
            directory = state_dir("Test", self.host)
        kitten = Path(__file__).parents[1] / "kzw" / "create_kitten.py"
        self.kitty("action", "--match", f"id:{window}", "kitten", str(kitten), "right")
        self.wait_for(lambda: len(pane_map(self.tree())) == 2 and
                      not (directory / "pending-create.json").exists() and
                      len(json.loads((directory / "panes.json").read_text())) == 2,
                      "managed split did not finish")
        panes = {value[0] for value in pane_map(self.tree()).values()}
        created = next(iter(panes - {target}))
        self.detach()
        with patch.dict(os.environ, self.env("a")):
            remote = remote_client.call(self.host, {"method": "workspace.get", "workspace": "Test"})
        pending = directory / "pending-create.json"
        pending.write_text(json.dumps({"kind": "right", "pane": created, "host": self.host,
                                       "target": target, "request_id": "a" * 32,
                                       "phase": "stage-recovery",
                                       "stage_hash": remote_client.snapshot_hash(remote["snapshot"], remote["panes"])}))
        self.attach("a", 2)
        self.assertEqual({value[0] for value in pane_map(self.tree()).values()}, panes)
        self.assertFalse(pending.exists())
        self.detach()

    def test_interrupted_attachment_reconnects_then_prunes_dead_session(self):
        ssh = shutil.which("ssh")
        self.assertIsNotNone(ssh)
        self.ssh_shim = Path(self.local.name) / "bin"
        self.ssh_shim.mkdir()
        gate = Path(self.local.name) / "block-ssh"
        wrapper = self.ssh_shim / "ssh"
        wrapper.write_text("#!/bin/sh\nif [ -e " + shlex.quote(str(gate)) +
                           " ]; then exit 255; fi\nexec " + shlex.quote(ssh) + ' "$@"\n')
        wrapper.chmod(0o700)
        self.kzw("a", "create", "Test", "--host", self.host)
        self.attach("a", 1)
        tree = self.tree()
        window = tree[0]["tabs"][0]["windows"][0]
        wid = window["id"]
        pane = pane_map(tree)[wid][0]
        self.wait_for(lambda: pane in zmx_command(self.host, "list", "--short").splitlines(), "session missing")
        self.kitty("send-text", "--match", f"id:{wid}", "echo KZW_BEFORE_RECONNECT\\r")
        self.wait_for(lambda: "\nKZW_BEFORE_RECONNECT\n" in self.kitty("get-text", "--match", f"id:{wid}", "--extent", "all"),
                      "shell was not ready")
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.local.name) / "a")}):
            directory = state_dir("Test", self.host)
        self.wait_for(lambda: (directory / "panes.json").exists() and
                      pane in json.loads((directory / "panes.json").read_text()), "snapshot missing")

        # Kill only the SSH client descended from this disposable Kitty pane,
        # never the shared SSH control connection or any unrelated process.
        def attachment_pid():
            return self.attachment_pid(window, pane)

        def coordinator_pids():
            output = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], text=True,
                                    capture_output=True, check=True).stdout
            matches = []
            for line in output.splitlines():
                fields = line.strip().split(None, 2)
                if (len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit() and
                        int(fields[1]) == self.process.pid and Path(fields[2].split()[0]).name == "ssh" and
                        "python3 -u -c" in fields[2] and self.host in fields[2]):
                    matches.append(int(fields[0]))
            return matches

        ssh_pid = self.wait_for(attachment_pid, "owned attachment process not found")
        coordinators = self.wait_for(coordinator_pids, "owned coordinator connection not found")
        gate.touch()
        for coordinator in set(coordinators + coordinator_pids()):
            try:
                os.kill(coordinator, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.kill(ssh_pid, signal.SIGKILL)
        self.wait_for(lambda: any(w.get("user_vars", {}).get("kzw_attach_ended") == "1"
                                 for o in self.tree() for t in o["tabs"] for w in t["windows"]),
                      "interruption was not detected")
        self.assertIn(pane, zmx_command(self.host, "list", "--short").splitlines())
        self.wait_for(lambda: self.kitty("get-text", "--match", f"id:{wid}", "--extent", "all").count(
            "retrying connection") >= 2, "connection was not retried while unavailable")
        self.assertEqual({v[0] for v in pane_map(self.tree()).values()}, {pane})
        self.assertEqual(json.loads((directory / "panes.json").read_text()), [pane])
        gate.unlink()
        self.wait_for(lambda: attachment_pid() not in (None, ssh_pid), "attachment did not retry")
        self.kitty("send-text", "--match", f"id:{wid}", "echo KZW_AFTER_RECONNECT\\r")
        self.wait_for(lambda: "\nKZW_AFTER_RECONNECT\n" in self.kitty("get-text", "--match", f"id:{wid}", "--extent", "all"),
                      "shell did not reconnect")
        self.assertEqual({v[0] for v in pane_map(self.tree()).values()}, {pane})
        reconnected_pid = self.wait_for(attachment_pid, "reconnected client missing")
        coordinators = self.wait_for(coordinator_pids, "coordinator did not reconnect")
        gate.touch()
        for coordinator in set(coordinators + coordinator_pids()):
            try:
                os.kill(coordinator, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.kill(reconnected_pid, signal.SIGKILL)
        self.wait_for(lambda: any(w.get("user_vars", {}).get("kzw_attach_ended") == "1"
                                 for o in self.tree() for t in o["tabs"] for w in t["windows"]),
                      "second interruption was not detected")
        # Only the disposable session observed in this isolated Kitty is killed.
        zmx_command(self.host, "kill", pane)
        time.sleep(2)
        self.assertEqual({v[0] for v in pane_map(self.tree()).values()}, {pane})
        self.assertEqual(json.loads((directory / "panes.json").read_text()), [pane])
        gate.unlink()
        self.process.wait(timeout=45)
        self.process = None
        self.assertIn("has no live panes", self.kzw("b", "attach", "Test", "--host", self.host))
