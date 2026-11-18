"""Opt-in isolated real Kitty + local coordinator + zmx lifecycle test (no SSH).

KZW_RUN_LOCAL_TAB_INTEGRATION=1 python3 -m unittest discover -s tests -p test_local_tabs_integration.py
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("KZW_RUN_LOCAL_TAB_INTEGRATION") == "1", "real Kitty/zmx is opt-in")
class LocalTabIntegrationTests(unittest.TestCase):
    def test_local_identity_survives_detach_restore_and_confirmed_kill_cleans_both(self):
        kitty, zmx = shutil.which("kitty"), shutil.which("zmx")
        if not kitty or not zmx:
            self.skipTest("Kitty and zmx are required")
        # macOS UNIX socket paths must fit in 104 bytes; the approved long temp
        # root cannot hold state/kzw/coordinator/server.sock beneath it.
        with tempfile.TemporaryDirectory(prefix="klt-", dir="/tmp") as temp:
            base = Path(temp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            config_dir = base / "kitty"
            config_dir.mkdir()
            shutil.copyfile(Path.home() / ".config/kitty/tab_bar.py", config_dir / "tab_bar.py")
            config = config_dir / "kitty.conf"
            config.write_text("tab_bar_style custom\ntab_bar_min_tabs 1\ntab_bar_align left\n"
                              "tab_bar_edge top\nshell_integration disabled\n"
                              "initial_window_width 140c\ninitial_window_height 30c\n")
            shim = bin_dir / "kitty"
            shim.write_text("#!/bin/sh\nif [ \"$1\" = @ ]; then exec " + shlex.quote(kitty) + " \"$@\"; fi\n"
                            "echo $$ >> " + shlex.quote(str(base / "kitty.pids")) + "\n"
                            "exec " + shlex.join([kitty, "--start-as=hidden", "--config", str(config)]) + ' "$@"\n')
            shim.chmod(0o700)
            env = dict(os.environ, XDG_STATE_HOME=str(base / "state"), ZMX_DIR=str(base / "zmx"),
                       SHELL="/bin/sh", KITTY_CONFIG_DIRECTORY=str(config_dir),
                       PATH=str(bin_dir) + os.pathsep + str(REPO / "bin") + os.pathsep + os.environ["PATH"],
                       PYTHONPATH=str(REPO))
            for key in ("KITTY_LISTEN_ON", "KITTY_WINDOW_ID", "KZW_RPC_SOCKET", "KZW_WORKSPACE",
                        "KZW_WORKSPACE_ORIGIN", "ZMX_SESSION", "ZMX_SESSION_PREFIX"):
                env.pop(key, None)
            directory = base / "state/kzw/Test"
            address = "unix:" + str(directory / "kitty.sock")
            processes = []
            owned = set()
            coordinator_pid = None

            def command(*args, input=None):
                result = subprocess.run([sys.executable, "-m", "kzw.cli", *args], env=env, text=True,
                                        input=input, capture_output=True, timeout=60)
                if result.returncode:
                    raise AssertionError(f"kzw {args}: {result.stderr}\n{result.stdout}")
                return result.stdout

            def raw(*args):
                return subprocess.run([kitty, "@", "--to", address, *args], env=env, text=True,
                                      capture_output=True, check=True, timeout=15).stdout

            def wait_until(predicate):
                deadline = time.monotonic() + 30
                while True:
                    try:
                        result = predicate()
                        if result:
                            return result
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    if time.monotonic() >= deadline:
                        self.fail("timed out; supervisor log:\n" + (base / "supervisor.log").read_text())
                    time.sleep(.1)

            probe = base / "probe.py"
            probe.write_text('''import json
from kittens.tui.handler import result_handler
def main(args): pass
@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map[target_window_id]
    manager = boss.os_window_map[window.os_window_id]
    manager.update_tab_bar_data()
    bar = manager.tab_bar
    bar.update(manager.tab_bar_data)
    extents = list(bar.tab_extents)
    def click(column):
        return bar.tab_id_at(bar.window_geometry.left + column * bar.cell_width, bar.window_geometry.top)
    hits = []
    if len(extents) == 2:
        hits = [click(extents[1].x.start + 2),
                click((extents[0].x.end + extents[1].x.start) // 2), click(bar.screen.columns - 2)]
    return json.dumps({"text": str(bar.screen.line(0)), "columns": bar.screen.columns,
                      "hits": hits,
                      "extents": [(item.tab_id, item.x.start, item.x.end) for item in bar.tab_extents]})
''')
            try:
                command("create", "Test")
                coordinator_pid = int(subprocess.run([
                    sys.executable, "-c", "from kzw.remote_client import call; print(call('', {'method':'server.info'})['pid'])"
                ], env=env, text=True, capture_output=True, check=True, timeout=15).stdout)
                with (base / "supervisor.log").open("w") as log:
                    def attach(action="attach"):
                        p = subprocess.Popen([sys.executable, "-m", "kzw.cli", action, "Test",
                                              "--hold", "--new-kitty-instance"], env=env, stdout=log, stderr=log)
                        processes.append(p)
                        wait_until(lambda: (directory / "panes.json").is_file() and json.loads(raw("ls")))
                        return p

                    first = attach()
                    tree = json.loads(raw("ls"))
                    source = tree[0]["tabs"][0]["windows"][0]["id"]
                    remote_pane = json.loads((directory / "panes.json").read_text())[0]
                    owned.add(remote_pane)
                    unrelated = int(raw("launch", "--type=os-window", "/bin/sh", "-c", "read -r unused"))
                    raw("kitten", "--match", f"id:{source}", str(REPO / "kzw/local_tab_kitten.py"), "--install-live")
                    raw("kitten", "--match", f"id:{source}", str(REPO / "kzw/local_tab_kitten.py"))
                    def find_local():
                        return next((w for o in json.loads(raw("ls")) for t in o["tabs"] for w in t["windows"]
                                     if w["user_vars"].get("kzw_local")), None)
                    local = wait_until(find_local)
                    pane = local["user_vars"]["kzw_local"]
                    owned.add(pane)
                    after = json.loads(raw("ls"))
                    workspace_os = next(o for o in after if any(
                        w["id"] == source for t in o["tabs"] for w in t["windows"]))
                    self.assertIn(local["id"], [w["id"] for t in workspace_os["tabs"] for w in t["windows"]])
                    other_os = next(o for o in after if any(
                        w["id"] == unrelated for t in o["tabs"] for w in t["windows"]))
                    self.assertNotEqual(workspace_os["id"], other_os["id"])
                    self.assertEqual(sum(len(t["windows"]) for t in other_os["tabs"]), 1)
                    raw("close-window", "--match", f"id:{unrelated}")
                    wait_until(lambda: pane in subprocess.run([zmx, "list", "--short"], env=env,
                               text=True, capture_output=True, check=True).stdout.splitlines())
                    pid_file = base / "shell.pid"
                    raw("send-text", "--match", f"id:{local['id']}",
                        "printf '%s' \"$$\" > " + shlex.quote(str(pid_file)) + "\n")
                    wait_until(pid_file.exists)
                    shell_pid = int(pid_file.read_text())
                    def tab_title():
                        return next((t["title"] for o in json.loads(raw("ls")) for t in o["tabs"]
                                     if any(w["id"] == local["id"] for w in t["windows"])), "")
                    wait_until(lambda: tab_title() == "sh")
                    raw("send-text", "--match", f"id:{local['id']}", "sleep 30\n")
                    wait_until(lambda: tab_title() == "sleep")
                    raw("send-text", "--match", f"id:{local['id']}", "\x03")
                    wait_until(lambda: tab_title() == "sh")
                    raw("kitten", "--match", f"id:{local['id']}", str(REPO / "kzw/local_tab_kitten.py"))
                    self.assertEqual(sum(bool(w["user_vars"].get("kzw_local")) for o in json.loads(raw("ls"))
                                         for t in o["tabs"] for w in t["windows"]), 1)
                    # Real scoped capture must exclude Local while it is selected.
                    command("save", "Test")
                    self.assertNotIn(pane, (directory / "workspace.kitty-session").read_text())
                    self.assertEqual(json.loads((directory / "panes.json").read_text()), [remote_pane])
                    command("detach", "Test")
                    first.wait(timeout=20)
                    os.kill(shell_pid, 0)
                    wait_until(lambda: not (directory / "kitty.sock").exists())
                    attach()
                    restored = wait_until(find_local)
                    self.assertEqual(restored["user_vars"]["kzw_local"], pane)
                    os.kill(shell_pid, 0)
                    # Exercise the real renderer, status cache and click extents.
                    os_id = json.loads(raw("ls"))[0]["id"]
                    payload = {"workspace": "Test", "origin": "", "metrics": {
                        "host": "test-host", "age_seconds": 0, "cpu_percent": 25, "ram_percent": 50}}
                    raw("kitten", "--match", f"id:{restored['id']}", str(REPO / "kzw/metrics_kitten.py"),
                        str(os_id), json.dumps(payload))
                    rendered = json.loads(raw("kitten", "--match", f"id:{restored['id']}", str(probe)))
                    self.assertIn("sh", rendered["text"])
                    self.assertIn("test-host", rendered["text"])
                    left, right = rendered["extents"]
                    self.assertGreater(right[1], left[2] + 2)
                    self.assertLessEqual(right[2] - right[1], 7)  # gap/status are not clickable as Local
                    self.assertEqual(rendered["hits"], [right[0], 0, 0])
                    current_source = next(w for o in json.loads(raw("ls")) for t in o["tabs"] for w in t["windows"]
                                          if w["user_vars"].get("kzw_pane") == remote_pane)
                    command("close-pane", "Test", "--window-id", str(current_source["id"]), input="y\n")
                    self.assertEqual((directory / "workspace.kitty-session").read_text(), "# kzw-empty-workspace\n")
                    command("detach", "Test")
                    processes[-1].wait(timeout=20)
                    os.kill(shell_pid, 0)
                    wait_until(lambda: not (directory / "kitty.sock").exists())
                    attach("new-pane")
                    wait_until(lambda: (directory / "workspace.kitty-session").read_text() != "# kzw-empty-workspace\n")
                    resumed = wait_until(find_local)
                    self.assertEqual(resumed["user_vars"]["kzw_local"], pane)
                    os.kill(shell_pid, 0)
                    raw("send-text", "--match", f"id:{resumed['id']}", "exit\n")
                    wait_until(lambda: not find_local())
                    self.assertNotIn(pane, subprocess.run([zmx, "list", "--short"], env=env,
                                                          text=True, capture_output=True, check=True).stdout.splitlines())
                    with sqlite3.connect(base / "state/kzw/coordinator/remote.sqlite3") as db:
                        self.assertEqual(db.execute("SELECT pane FROM local_tabs").fetchall(), [])
                    owned.update(json.loads((directory / "panes.json").read_text()))
                    command("detach", "Test")
                    processes[-1].wait(timeout=20)
                    command("kill", "Test", input="y\n")
                    live = subprocess.run([zmx, "list", "--short"], env=env, text=True,
                                          capture_output=True, check=True).stdout.splitlines()
                    self.assertFalse(owned.intersection(live))
            finally:
                try:
                    raw("close-os-window", "--match", "all")
                except (OSError, subprocess.SubprocessError):
                    pass  # This socket belongs only to our isolated Kitty.
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if (base / "kitty.pids").exists():
                    for pid in (base / "kitty.pids").read_text().splitlines():
                        try:
                            os.kill(int(pid), 15)  # Exact PID recorded by our disposable launch shim.
                        except ProcessLookupError:
                            pass
                database = base / "state/kzw/coordinator/remote.sqlite3"
                if database.is_file():
                    with sqlite3.connect(database) as db:
                        for (panes,) in db.execute("SELECT panes FROM workspaces WHERE name='Test'"):
                            owned.update(json.loads(panes))
                        owned.update(pane for (pane,) in db.execute("SELECT pane FROM local_tabs"))
                        owned.update(pane for (pane,) in db.execute("SELECT pane FROM reservations WHERE workspace='Test'"))
                for pane in owned:
                    subprocess.run([zmx, "kill", pane], env=env, capture_output=True, timeout=15)
                if coordinator_pid is not None:
                    for attempt in range(30):
                        try:
                            command("server-stop", input="y\n")
                            break
                        except (OSError, AssertionError, subprocess.SubprocessError):
                            if attempt == 29:
                                os.kill(coordinator_pid, 15)
                                self.fail("isolated coordinator needed forced cleanup after 3s")
                            time.sleep(.1)


if __name__ == "__main__":
    unittest.main()
