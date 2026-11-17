"""Opt-in real Kitty export check with fake pane launchers; no zmx or SSH.

KZW_RUN_SESSION_EXPORT_INTEGRATION=1 python3 -m unittest discover -s tests -p test_session_export_integration.py
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
PANE = "kzw-" + "a" * 32
OTHER = "kzw-" + "b" * 32


@unittest.skipUnless(os.environ.get("KZW_RUN_SESSION_EXPORT_INTEGRATION") == "1", "real Kitty export is opt-in")
class SessionExportIntegrationTests(unittest.TestCase):
    def test_scoped_export_keeps_callbacks_layout_processes_and_complete_panes(self):
        kitty = shutil.which("kitty")
        if not kitty:
            self.skipTest("kitty is required")
        with tempfile.TemporaryDirectory(prefix="kzw-export-", dir=os.environ.get("OPENCODE_TEST_TMP")) as temp:
            base = Path(temp)
            launcher = base / "kzw-pane"
            launcher.write_text("#!/bin/sh\nprintf ready\nread -r unused\n")
            launcher.chmod(0o700)
            session = base / "Test.session"
            session.write_text(
                "new_tab Test\nlaunch --var=kzw_pane=" + PANE + " --var=kzw_host= " +
                shlex.join([str(launcher), "--pane", PANE, "--host", "", "--existing"]) + "\n")
            probe = base / "hooks.py"
            probe.write_text('''import json, runpy, sys
from kittens.tui.handler import result_handler
from kitty.session import default_save_as_session_opts
def main(args): pass
@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    window = boss.window_id_map[target_window_id]
    if args[2] == "install":
        for name in ("actions_on_close", "actions_on_focus_change", "actions_on_removal"):
            def callback(*args): window._export_test_calls += 1
            getattr(window, name).append(callback)
        window._export_test_calls = 0
        window._export_test_hooks = tuple(getattr(window, name) for name in (
            "actions_on_close", "actions_on_focus_change", "actions_on_removal"))
    tm = boss.os_window_map[window.os_window_id]
    native = tm.serialize_state_as_session("", None, default_save_as_session_opts(), is_first=True)
    before = json.dumps(tm.serialize_state(), sort_keys=True)
    callbacks = tuple(tuple(items) for items in window._export_test_hooks)
    sys.path.insert(0, args[1])
    helper = runpy.run_path(args[1] + "/kzw/scoped_session_kitten.py")
    snapshot = json.loads(helper["handle_result"](
        ["probe", str(window.os_window_id), "--with-tree"], None, window.id, boss))
    after = json.dumps(tm.serialize_state(), sort_keys=True)
    identical = all(getattr(window, name) is items and tuple(items) == saved
                    for name, items, saved in zip(
                        ("actions_on_close", "actions_on_focus_change", "actions_on_removal"),
                        window._export_test_hooks, callbacks))
    window._export_test_hooks[2][0](window)
    return json.dumps({"snapshot": snapshot, "native_launches": sum(
                          line.startswith("launch ") for line in native),
                       "layout_unchanged": before == after,
                       "callbacks_unchanged": identical, "callback_calls": window._export_test_calls,
                       "method_restored": "as_launch_command" not in vars(window),
                       "pid": window.child.pid, "native_keys": repr(boss.mappings.global_shortcuts)})
''')
            socket = "unix:" + str(base / "kitty.sock")
            env = dict(os.environ)
            env.pop("KITTY_LISTEN_ON", None)
            env.pop("KITTY_WINDOW_ID", None)
            with (base / "kitty.log").open("w+") as log:
                process = subprocess.Popen([
                    kitty, "--start-as=hidden", "--config", "NONE", "--listen-on", socket,
                    "-o", "allow_remote_control=socket-only", "-o", "shell_integration=disabled",
                    "--session", str(session),
                ], env=env, stdout=log, stderr=log)
                try:
                    def raw(*args):
                        return subprocess.run([kitty, "@", "--to", socket, *args], text=True,
                                              capture_output=True, check=True, timeout=10).stdout

                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            tree = json.loads(raw("ls"))
                            break
                        except (subprocess.SubprocessError, json.JSONDecodeError):
                            if process.poll() is not None or time.monotonic() > deadline:
                                log.seek(0)
                                self.fail("isolated Kitty did not start: " + log.read())
                            time.sleep(.1)
                    source = tree[0]["tabs"][0]["windows"][0]["id"]
                    second = int(raw("launch", "--match", f"window_id:{source}", "--type=window",
                                     "--location=hsplit", "--var=kzw_pane=" + OTHER, "--var=kzw_host=",
                                     str(launcher), "--pane", OTHER, "--host", "", "--existing").strip())
                    unrelated = int(raw("launch", "--type=os-window", "--os-window-state=minimized",
                                        "/bin/sh", "-c", "read -r unused").strip())
                    before = json.loads(raw("ls"))
                    first = json.loads(raw("kitten", "--match", f"id:{source}", str(probe), str(REPO), "install"))
                    self.assertEqual(first["native_launches"], 1)  # reproduces Kitty's omission
                    from kzw.core import pane_map, rewrite_export
                    panes = pane_map(first["snapshot"]["tree"])
                    self.assertEqual(set(panes), {source, second})
                    restored = rewrite_export(first["snapshot"]["session"], panes, "kzw-pane")
                    self.assertEqual(sum(line.startswith("launch ") for line in restored.splitlines()), 2)
                    self.assertIn("--existing", restored)
                    for result in (first, json.loads(raw("kitten", "--match", f"id:{source}",
                                                        str(probe), str(REPO), "again"))):
                        self.assertTrue(result["callbacks_unchanged"])
                        self.assertTrue(result["layout_unchanged"])
                        self.assertTrue(result["method_restored"])
                        self.assertGreater(result["callback_calls"], 0)
                        self.assertEqual(result["pid"], first["pid"])
                        self.assertEqual(result["native_keys"], first["native_keys"])
                    after = json.loads(raw("ls"))
                    def identities(items):
                        return {(w["id"], w["pid"]) for osw in items for tab in osw["tabs"] for w in tab["windows"]}
                    self.assertEqual(identities(before), identities(after))
                    self.assertIn(unrelated, {window for window, _ in identities(after)})
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
