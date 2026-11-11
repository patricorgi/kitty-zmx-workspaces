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
import shlex
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
        self.env["SHELL"] = "/bin/sh"
        self.env["XDG_STATE_HOME"] = str(self.state)
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
            # A failed creation can leave the disposable supervisor waiting on
            # its journal after Kitty has gone. Identify it by this test's lock.
            lock = self.state / "kzw" / "Test" / "lock"
            if lock.exists():
                holders = subprocess.run(["lsof", "-t", str(lock)], capture_output=True, text=True).stdout.split()
                for pid in holders:
                    command = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output=True, text=True).stdout
                    if "-m kzw.cli attach Test" not in command and "-m kzw.cli start Test" not in command:
                        self.fail("unexpected test lock owner " + pid)
                    os.kill(int(pid), signal.SIGTERM)
            if (self.state / "kzw" / "coordinator" / "server.sock").exists():
                for _ in range(20):
                    stopped = self.kzw("server-stop", input="y\n")
                    if not stopped.returncode:
                        break
                    time.sleep(0.1)
                if stopped.returncode:
                    self.fail("isolated test coordinator cleanup failed at " + str(self.tmp) + ": " + stopped.stderr)
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

    def kzw(self, *args, timeout=120, input=None):
        return subprocess.run([sys.executable, "-m", "kzw.cli", *args], cwd=REPO, env=self.env,
                              capture_output=True, text=True, timeout=timeout, input=input)

    def tab_style(self):
        output = self.raw("kitten", str(self.probe))
        return json.loads(output)

    def test_tab_styling_preserves_shortcuts_and_paste_across_repeated_apply_restore(self):
        if sys.platform != "darwin":
            self.skipTest("native macOS shortcuts are required")
        from kzw.titles import TAB_TITLE_TEMPLATE as TEMPLATE

        self.baseline = self.sessions()
        probe = str(REPO / "tests" / "kitty_shortcuts_probe.py")
        style = str(REPO / "kzw" / "tab_style_kitten.py")
        state = str(self.tmp / "Test-style.json")
        window = json.loads(self.raw("ls"))[0]["tabs"][0]["windows"][0]
        match = "id:" + str(window["id"])
        token = secrets.token_hex(16)
        self.raw("set-tab-title", "--match", "window_id:" + str(window["id"]), "Test")
        self.raw("set-user-vars", "--match", match, "kzw_paste_probe=" + token)
        reader = self.tmp / "paste_reader.py"
        reader.write_text(
            "import os, tty\n"
            "tty.setraw(0)\n"
            "os.write(1, b'\\x1b[?2004l\\r\\nPASTE_READY\\r\\n')\n"
            "pending = b''\n"
            "while True:\n"
            "    chunk = os.read(0, 4096)\n"
            "    if not chunk: break\n"
            "    pending += chunk\n"
            "    while b'kzw-paste-probe' in pending:\n"
            "        pending = pending.split(b'kzw-paste-probe', 1)[1]\n"
            "        os.write(1, b'RECEIVED:kzw-paste-probe\\r\\n')\n")
        self.raw("send-text", "--match", match, "exec " + shlex.join([sys.executable, "-u", str(reader)]) + "\n")

        def wait_for(text, count=1):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.raw("get-text", "--match", match).count(text) >= count:
                    return
                time.sleep(0.1)
            self.fail("disposable paste reader did not receive " + text + ": " + self.raw("get-text", "--match", match)[-2000:])

        # Require a complete output line, not the echoed Python command.
        wait_for("\nPASTE_READY\n")
        before = json.loads(self.raw("kitten", probe))
        self.assertEqual(before["global"]["paste_from_clipboard"], "SingleKey(mods=8, key=118)")
        before_tree = json.loads(self.raw("ls"))
        count = 0
        for action in ("apply", "apply", "restore", "apply", "restore"):
            self.raw("kitten", style, action, state)
            after = json.loads(self.raw("kitten", probe))
            self.assertEqual(after["global"], before["global"])
            self.assertEqual(after["modes"], before["modes"])
            templates = [TEMPLATE, TEMPLATE] if action == "apply" else before["templates"]
            self.assertEqual(after["templates"], templates)
            self.assertTrue(all(value == templates for value in after["renderers"].values()))
            self.assertEqual(json.loads(self.raw("kitten", "--match", match, probe, "paste", token)), {"consumed": True})
            count += 1
            wait_for("RECEIVED:kzw-paste-probe", count)
        after_tree = json.loads(self.raw("ls"))
        self.assertEqual(after_tree[0]["tabs"][0]["layout_state"], before_tree[0]["tabs"][0]["layout_state"])
        self.assertEqual(after_tree[0]["tabs"][0]["windows"][0]["pid"], before_tree[0]["tabs"][0]["windows"][0]["pid"])

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

    def test_background_create_attach_inherits_appearance_with_prior_failure_log(self):
        self.baseline = self.sessions()
        unrelated = json.loads(self.raw("ls"))[0]
        source = unrelated["tabs"][0]["windows"][0]
        self.env["KITTY_WINDOW_ID"] = str(source["id"])
        directory = self.state / "kzw" / "Test"
        directory.mkdir(parents=True)
        log = directory / "supervisor.log"
        log.write_text("previous startup failed\n")
        result = self.kzw("create", "Test", "--attach", "--inherit-appearance")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("started in the background", result.stdout)
        self.assertTrue(log.read_text().startswith("previous startup failed\n"))
        view = json.loads((directory / "view.json").read_text())
        self.assertEqual(view["instance_pid"], self.instance.pid)
        self.assertEqual(set(view["panes"]), self.sessions() - self.baseline)
        self.assertEqual(len(view["panes"]), 1)
        result = self.kzw("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Test")
        result = self.kzw("detach", "Test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = json.loads(self.raw("ls"))
        self.assertEqual([ow["id"] for ow in after], [unrelated["id"]])
        self.assertEqual(after[0]["tabs"][0]["windows"][0]["pid"], source["pid"])

    def test_coordinator_ctrl_d_split_nonzero_tab_and_final_pane(self):
        self.baseline = self.sessions()
        unrelated = json.loads(self.raw("ls"))[0]
        result = self.kzw("create", "Test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.kzw("attach", "Test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        directory = self.state / "kzw" / "Test"

        def panes():
            return {w["user_vars"]["kzw_pane"]: w for ow in json.loads(self.raw("ls"))
                    for tab in ow["tabs"] for w in tab["windows"] if w.get("user_vars", {}).get("kzw_pane")}

        def wait_for(predicate):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.1)
            self.fail("exit lifecycle timed out: " + (directory / "supervisor.log").read_text()[-4000:])

        def manifest():
            return set(json.loads((directory / "panes.json").read_text()))

        first = next(iter(panes()))
        for kind in ("right", "tab"):
            anchor = panes()[first]
            result = self.kzw("_managed-create", "Test", kind, str(anchor["id"]), first)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        wait_for(lambda: len(manifest()) == 3 and manifest() == self.sessions() and len(panes()) == 3)
        original = panes()
        split, tab = sorted(set(original) - {first}, key=lambda p: original[p]["id"])

        # Kill only the disposable pane's local zmx client, never its daemon.
        children = subprocess.check_output(["ps", "-axo", "pid=,ppid=,comm="], text=True)
        clients = [int(line.split()[0]) for line in children.splitlines()
                   if len(line.split()) >= 3 and int(line.split()[1]) == original[split]["pid"]
                   and Path(line.split()[2]).name == "zmx"]
        self.assertEqual(len(clients), 1)
        os.kill(clients[0], signal.SIGKILL)
        wait_for(lambda: panes()[split]["user_vars"].get("kzw_attach_ended") == "1")
        self.assertEqual(manifest(), set(original))
        self.assertEqual(self.sessions(), set(original))
        wait_for(lambda: panes()[split]["user_vars"].get("kzw_attach_ended") == "0")

        elapsed = []
        for pane, text in ((split, "\x04"), (tab, "exit 7\r"), (first, "\x04")):
            started = time.monotonic()
            self.raw("send-text", "--match", "id:" + str(original[pane]["id"]), text)
            wait_for(lambda: pane not in panes() and pane not in manifest() and
                     not (directory / "pending-exit.json").exists())
            elapsed.append(round(time.monotonic() - started, 2))
            for survivor, window in panes().items():
                self.assertEqual(window["pid"], original[survivor]["pid"])
        wait_for(lambda: not (directory / "view.json").exists())
        self.assertEqual(self.sessions(), self.baseline)
        self.assertEqual((directory / "workspace.kitty-session").read_text(), "# kzw-empty-workspace\n")
        remaining = json.loads(self.raw("ls"))
        self.assertEqual([w["id"] for w in remaining], [unrelated["id"]])
        self.assertEqual(remaining[0]["tabs"][0]["windows"][0]["pid"], unrelated["tabs"][0]["windows"][0]["pid"])
        print("Confirmed split/tab/final exit close latency (s):", elapsed)


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
        if (self.state / "kzw" / "coordinator" / "server.sock").exists():
            for _ in range(20):
                stopped = subprocess.run([sys.executable, "-m", "kzw.cli", "server-stop"], cwd=REPO, env=self.env,
                                         input="y\n", capture_output=True, text=True, timeout=30)
                if not stopped.returncode:
                    break
                time.sleep(0.1)
            if stopped.returncode:
                self.fail("isolated coordinator cleanup failed at " + str(self.tmp) + ": " + stopped.stderr)
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

    def test_coordinator_final_exit_closes_private_instance_and_finishes_journal(self):
        self.env["SHELL"] = "/bin/sh"
        for args in (("create", "Test"), ("attach", "Test", "--new-kitty-instance")):
            result = self.kzw(*args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tree = json.loads(self.raw("ls"))
        window = tree[0]["tabs"][0]["windows"][0]
        self.raw("send-text", "--match", "id:" + str(window["id"]), "\x04")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (self.directory / "workspace.kitty-session").read_text() == "# kzw-empty-workspace\n" and not (self.directory / "pending-exit.json").exists():
                with open(self.directory / "lock", "a+") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        break
            time.sleep(0.1)
        else:
            self.fail("private final exit did not finish: " + (self.directory / "supervisor.log").read_text()[-4000:])
        self.assertEqual(self.sessions(), self.baseline)


if __name__ == "__main__":
    unittest.main()
