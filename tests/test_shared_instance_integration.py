"""Opt-in same-instance lifecycle test with a disposable hidden Kitty.

Run with KZW_RUN_INTEGRATION=1. Uses a unique single-instance group, an
isolated state home, and a *Test* workspace. It never touches the user's Kitty
windows or existing zmx sessions.
"""

import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real integration is opt-in")
class SharedInstanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        for tool in ("kitty", "zmx", "lsof"):
            if not shutil.which(tool):
                self.skipTest(tool + " is required")
        self.tmp = Path(tempfile.mkdtemp(prefix="kzw-si-", dir=Path.home() / ".cache"))
        self.sock = self.tmp / "kitty.sock"
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.zmx_dir = self.tmp / "zmx"
        self.env = os.environ.copy()
        self.env["ZMX_DIR"] = str(self.zmx_dir)
        self.group = "kzw-it-" + secrets.token_hex(6)
        session = self.tmp / "keep.session"
        # One unmanaged OS window proves that detach only closes our workspace.
        session.write_text("new_tab Keep\nlaunch /bin/sh\n")
        self.instance = subprocess.Popen(
            ["kitty", "--single-instance", "--instance-group", self.group,
             "--start-as=hidden", "--listen-on", "unix:" + str(self.sock),
             "-c", "NONE", "-o", "allow_remote_control=socket-only",
             "--session", str(session)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env)
        self.env.update({
            "PATH": str(REPO / "bin") + os.pathsep + self.env.get("PATH", ""),
            "PYTHONPATH": str(REPO) + (os.pathsep + self.env["PYTHONPATH"] if self.env.get("PYTHONPATH") else ""),
            "XDG_STATE_HOME": str(self.state),
            "KITTY_PID": str(self.instance.pid),
            "KITTY_LISTEN_ON": "unix:" + str(self.sock),
        })
        self.probe = self.tmp / "probe.py"
        self.probe.write_text(
            "import json\n"
            "from kittens.tui.handler import result_handler\n"
            "from kitty.boss import get_options\n"
            "def main(args): pass\n"
            "@result_handler(no_ui=True)\n"
            "def handle_result(args, answer, target_window_id, boss):\n"
            "    o = get_options()\n"
            "    return json.dumps({'tab': o.tab_title_template,"
            " 'font': list(o.active_tab_font_style)})\n")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.instance.poll() is not None:
                self.fail("hidden single-instance Kitty exited before serving")
            try:
                if self.raw("ls").strip():
                    break
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
            time.sleep(0.1)
        else:
            self.fail("hidden single-instance Kitty did not start")

    def tearDown(self):
        try:
            if (self.state / "kzw" / "Test" / "view.json").exists():
                subprocess.run([sys.executable, "-m", "kzw.cli", "detach", "Test"],
                               cwd=REPO, env=self.env, capture_output=True, text=True, timeout=60)
        finally:
            try:
                subprocess.run(["kitty", "@", "--to", "unix:" + str(self.sock), "quit"],
                               capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                self.instance.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.instance.kill()
                self.instance.wait(timeout=10)
            self.cleanup_sessions()
            shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, *args):
        return subprocess.run(["kitty", "@", "--to", "unix:" + str(self.sock), *args],
                              capture_output=True, text=True, check=True, timeout=15).stdout

    def sessions(self):
        output = subprocess.run(["zmx", "list", "--short"], env=self.env, capture_output=True, text=True, timeout=15).stdout
        return {line.strip() for line in output.splitlines() if line.strip()}

    def cleanup_sessions(self):
        baseline = getattr(self, "baseline", set())
        extra = self.sessions() - baseline
        if extra:
            subprocess.run(["zmx", "kill", *sorted(extra), "--force"], env=self.env, capture_output=True, text=True, timeout=30)

    def kzw(self, *args, timeout=120):
        return subprocess.run([sys.executable, "-m", "kzw.cli", *args], cwd=REPO, env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def tab_style(self):
        output = self.raw("kitten", str(self.probe))
        return json.loads(output)

    def test_attach_in_shared_instance_and_detach_only_workspace(self):
        self.baseline = self.sessions()
        before_style = self.tab_style()
        self.assertNotIn("kzw-pane", before_style["tab"])
        unrelated = [window for window in json.loads(self.raw("ls"))
                     if not any(w.get("user_vars", {}).get("kzw_pane")
                                for tab in window.get("tabs", []) for w in tab.get("windows", []))]
        self.assertEqual(len(unrelated), 1)
        unrelated_id = unrelated[0]["id"]

        result = self.kzw("start", "Test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        directory = self.state / "kzw" / "Test"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (directory / "view.json").exists():
            time.sleep(0.1)
        view = json.loads((directory / "view.json").read_text())
        self.assertEqual(view["address"], "unix:" + str(self.sock))
        self.assertEqual(view["instance_pid"], self.instance.pid)
        self.assertNotEqual(view["os_window_id"], unrelated_id)
        self.assertEqual(set(view["panes"]), self.sessions() - self.baseline)
        attached_style = self.tab_style()
        self.assertIn("kzw-pane", attached_style["tab"])
        self.assertEqual(attached_style["font"], before_style["font"])

        windows = {window["id"]: window for window in json.loads(self.raw("ls"))}
        self.assertEqual(set(windows), {unrelated_id, view["os_window_id"]})
        workspace_panes = {w["user_vars"]["kzw_pane"]
                           for tab in windows[view["os_window_id"]]["tabs"]
                           for w in tab["windows"]}
        self.assertEqual(workspace_panes, set(view["panes"]))

        result = self.kzw("detach", "Test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((directory / "view.json").exists())
        self.assertFalse((directory / "kitty.sock").exists())
        self.assertEqual({window["id"] for window in json.loads(self.raw("ls"))}, {unrelated_id})
        self.assertEqual(self.tab_style(), before_style)

        # The background supervisor must release the workspace lock by itself.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with open(directory / "lock", "a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    break
            time.sleep(0.1)
        else:
            self.fail("background supervisor did not exit after detach")
        self.assertIsNone(self.instance.poll())


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real integration is opt-in")
class NewInstanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        for tool in ("kitty", "zmx"):
            if not shutil.which(tool):
                self.skipTest(tool + " is required")
        if sys.platform != "darwin" or not Path("/Applications/kitty.app").is_dir():
            self.skipTest("background Kitty launch requires /Applications/kitty.app")
        self.tmp = Path(tempfile.mkdtemp(prefix="kzw-ni-", dir=Path.home() / ".cache"))
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.directory = self.state / "kzw" / "Test"
        self.sock = self.directory / "kitty.sock"
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(REPO / "bin") + os.pathsep + self.env.get("PATH", ""),
            "PYTHONPATH": str(REPO) + (os.pathsep + self.env["PYTHONPATH"] if self.env.get("PYTHONPATH") else ""),
            "XDG_STATE_HOME": str(self.state),
            "ZMX_DIR": str(self.tmp / "zmx"),
            "KZW_TEST_KITTY_BACKGROUND": "1",
        })
        output = subprocess.run(["zmx", "list", "--short"], env=self.env, capture_output=True, text=True, timeout=15).stdout
        self.baseline = {line.strip() for line in output.splitlines() if line.strip()}

    def tearDown(self):
        subprocess.run([sys.executable, "-m", "kzw.cli", "detach", "Test"], cwd=REPO, env=self.env,
                       capture_output=True, text=True, timeout=60)
        if self.sock.exists():
            try:
                subprocess.run(["kitty", "@", "--to", "unix:" + str(self.sock), "quit"],
                               capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        # Launch Services can keep the test Kitty alive after its last window
        # closes. Match the exact unique socket argument, never another Kitty.
        listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True)
        marker = "--listen-on unix:" + str(self.sock)
        for line in listing.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and fields[1].startswith("/Applications/kitty.app/Contents/MacOS/kitty ") and marker in fields[1]:
                try:
                    os.kill(int(fields[0]), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        extra = self.sessions() - self.baseline
        if extra:
            subprocess.run(["zmx", "kill", *sorted(extra), "--force"], env=self.env, capture_output=True, text=True, timeout=30)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sessions(self):
        output = subprocess.run(["zmx", "list", "--short"], env=self.env, capture_output=True, text=True, timeout=15).stdout
        return {line.strip() for line in output.splitlines() if line.strip()}

    def raw(self, *args):
        return subprocess.run(["kitty", "@", "--to", "unix:" + str(self.sock), *args],
                              capture_output=True, text=True, check=True, timeout=15).stdout

    def kzw(self, *args, timeout=120):
        return subprocess.run([sys.executable, "-m", "kzw.cli", *args], cwd=REPO, env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def test_new_instance_attach_and_detach(self):
        result = self.kzw("start", "Test", "--new-kitty-instance")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.sock.exists():
                try:
                    if self.raw("ls").strip():
                        break
                except (OSError, subprocess.SubprocessError):
                    pass
            time.sleep(0.1)
        else:
            self.fail("separate Kitty instance did not open the workspace")
        tree = json.loads(self.raw("ls"))
        panes = {w["user_vars"]["kzw_pane"] for window in tree
                 for tab in window["tabs"] for w in tab["windows"]}
        self.assertEqual(panes, self.sessions() - self.baseline)

        probe = self.tmp / "probe.py"
        probe.write_text(
            "import json\nfrom kittens.tui.handler import result_handler\n"
            "from kitty.boss import get_options\n"
            "def main(args): pass\n"
            "@result_handler(no_ui=True)\n"
            "def handle_result(args, answer, target_window_id, boss):\n"
            "    return json.dumps({'tab': get_options().tab_title_template})\n")
        template = json.loads(self.raw("kitten", str(probe)))["tab"]
        self.assertIn("{index}", template)
        self.assertIn("{fmt.nobold}", template)

        result = self.kzw("detach", "Test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with open(self.directory / "lock", "a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    break
            time.sleep(0.1)
        else:
            self.fail("separate-instance supervisor did not exit")


if __name__ == "__main__":
    unittest.main()
