"""Opt-in remote-origin smoke test with isolated local and remote state."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

from kzw.cli import zmx_command
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
        return env

    def kzw(self, client, *args):
        result = subprocess.run(["kzw", *args], env=self.env(client), capture_output=True,
                                text=True, timeout=60)
        if result.returncode:
            raise AssertionError(f"kzw {args} failed: {result.stderr}")
        return result.stdout

    def kitty(self, *args):
        socket = Path(self.local.name) / self.state / "kzw" / "Test" / "kitty.sock"
        return subprocess.run(["kitty", "@", "--to", f"unix:{socket}", *args],
                              capture_output=True, text=True, check=True, timeout=10).stdout

    def tree(self):
        tree = json.loads(self.kitty("ls"))
        self.owned.update(pane for pane, _ in pane_map(tree).values())
        return tree

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

    def attach(self, client, count):
        self.state = client
        with self.log.open("a") as log:
            self.process = subprocess.Popen(["kzw", "attach", "Test", "--host", self.host],
                                            env=self.env(client), stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log)
        self.wait_for(lambda: len(pane_map(self.tree())) == count, "remote workspace did not attach")

    def detach(self):
        self.kzw(self.state, "detach", "Test")
        self.process.wait(timeout=20)
        self.process = None

    def cleanup(self):
        if self.process and self.process.poll() is None:
            try:
                self.tree()
                self.kitty("close-window", "--match", "all")
                self.process.wait(timeout=10)
            except (OSError, ValueError, subprocess.SubprocessError):
                print("kzw remote integration: test Kitty may still be open")
        for pane in self.owned:
            try:
                if pane in zmx_command(self.host, "list", "--short").splitlines():
                    zmx_command(self.host, "kill", pane)
            except (OSError, subprocess.SubprocessError):
                print(f"kzw remote integration: manual cleanup may be needed for {pane}")
        # Remove only the private SQLite files and directories created by this
        # test; never recursively remove or touch any other remote path.
        script = ("import os,sys; p=sys.argv[1]; d=os.path.join(p,'kzw'); "
                  "[(os.unlink(os.path.join(d,n)) if os.path.isfile(os.path.join(d,n)) else None) "
                  "for n in ('remote.sqlite3','remote.sqlite3-journal','remote.sqlite3-wal','remote.sqlite3-shm')]; "
                  "os.rmdir(d); os.rmdir(p)")
        try:
            subprocess.run(["ssh", "-o", "BatchMode=yes", self.host,
                            "python3 -c " + shlex.quote(script) + " " + shlex.quote(self.remote)],
                           capture_output=True, text=True, check=True, timeout=20)
        except subprocess.SubprocessError:
            print(f"kzw remote integration: test state remains at {self.remote}")
        self.local.cleanup()

    def test_create_and_attach_from_fresh_local_state(self):
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
        self.attach("b", 2)
        restored = pane_map(self.tree())
        self.assertEqual({v[0] for v in restored.values()}, ids)
        first_id = next(key for key, value in restored.items() if value[0] == first_pane)
        self.wait_for(lambda: "REMOTE_WORKSPACE_TEST" in self.kitty("get-text", "--match", f"id:{first_id}", "--extent", "all"), "restored output missing")
        second_id = next(key for key in restored if key != first_id)
        second_pane = restored[second_id][0]
        self.kitty("send-text", "--match", f"id:{second_id}", "echo EXIT_READY\\r")
        self.wait_for(lambda: "\nEXIT_READY\n" in self.kitty("get-text", "--match", f"id:{second_id}", "--extent", "all"),
                      "second remote pane was not ready")
        self.kitty("send-text", "--match", f"id:{second_id}", "\\x04")
        self.wait_for(lambda: len(pane_map(self.tree())) == 1, "exited remote pane not removed")
        manifest = Path(self.local.name) / "b" / "kzw" / "Test" / "panes.json"
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
