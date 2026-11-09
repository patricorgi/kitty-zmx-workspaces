"""Opt-in, destructive-only-to-own-panes real Kitty/SSH/zmx smoke test.

Set KZW_RUN_INTEGRATION=1 and KZW_INTEGRATION_HOST to an SSH alias, and put
the installed kzw and kzw-pane scripts on PATH. This opens Kitty windows.
Only pane identities observed in this test's isolated Kitty instance are
eligible for cleanup. A hard crash can leave those sessions behind.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from kzw.cli import zmx_command
from kzw.core import focused_pane, pane_map


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "set KZW_RUN_INTEGRATION=1 for real Kitty/SSH/zmx")
class RealWorkspaceLifecycle(unittest.TestCase):
    def setUp(self):
        self.host = os.environ.get("KZW_INTEGRATION_HOST")
        if not self.host or not shutil.which("kitty") or not shutil.which("kzw") or not shutil.which("kzw-pane"):
            self.skipTest("requires KZW_INTEGRATION_HOST, kitty, kzw and kzw-pane on PATH")
        # Keep the Unix socket path short enough for macOS's sun_path limit.
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="ki-", dir=cache)
        self.env = os.environ.copy()
        self.env["XDG_STATE_HOME"] = self.temp.name
        self.name = "integration-" + uuid.uuid4().hex[:12]
        self.path = Path(self.temp.name) / "kzw" / self.name
        self.socket = self.path / "kitty.sock"
        self.owned = set()
        self.process = None
        self.addCleanup(self.cleanup)

    def command(self, *args):
        return subprocess.run(["kzw", *args], env=self.env, text=True, capture_output=True, check=True, timeout=60).stdout

    def kitty(self, *args):
        return subprocess.run(["kitty", "@", "--to", f"unix:{self.socket}", *args], env=self.env,
                              text=True, capture_output=True, check=True, timeout=10).stdout

    def tree(self):
        tree = json.loads(self.kitty("ls"))
        self.owned.update(pane for pane, _ in pane_map(tree).values())
        return tree

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + 30
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                last_error = exc
            time.sleep(0.1)
        self.fail(f"{message}: {last_error}")

    def start(self, count):
        self.process = subprocess.Popen(["kzw", "start", self.name, "--host", self.host],
                                        env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        self.wait_for(lambda: len(pane_map(self.tree())) == count, "workspace did not restore")

    def stop(self):
        self.command("detach", self.name)
        self.process.wait(timeout=15)
        self.process = None

    def remote_sessions(self):
        return set(zmx_command(self.host, "list", "--short").splitlines())

    def remote_pids(self):
        text = zmx_command(self.host, "list")
        return {match.group(1): int(match.group(2)) for match in
                re.finditer(r"name=(kzw-[0-9a-f]{32})\s+pid=(\d+)", text)}

    def cleanup(self):
        # Never act on sessions merely found on the remote host. Only IDs read
        # from this test's isolated Kitty are eligible for removal.
        if self.process and self.process.poll() is None:
            try:
                self.tree()
                self.kitty("close-window", "--match", "all")
                self.process.wait(timeout=10)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        for pane in self.owned:
            try:
                if pane in self.remote_sessions():
                    zmx_command(self.host, "kill", pane)
            except (OSError, subprocess.SubprocessError):
                # Report rather than silently destroying or assuming cleanup.
                print(f"kzw integration: manual cleanup may be needed for {pane}")
        self.temp.cleanup()

    def test_splits_tabs_and_reattach(self):
        self.start(1)
        first = next(iter(pane_map(self.tree())))
        second_tab_first = int(self.kitty("launch", "--type=tab", "--tab-title=kzw-integration"))
        right = int(self.kitty("launch", "--match", f"id:{second_tab_first}", "--next-to", f"id:{second_tab_first}", "--location=vsplit"))
        bottom = int(self.kitty("launch", "--match", f"id:{second_tab_first}", "--next-to", f"id:{second_tab_first}", "--location=hsplit"))
        self.wait_for(lambda: len(pane_map(self.tree())) == 4, "new split metadata missing")
        initial = pane_map(self.tree())
        right_pane, bottom_pane = initial[right][0], initial[bottom][0]
        expected = {v[0] for v in initial.values()}
        self.wait_for(lambda: expected.issubset(self.remote_sessions()), "remote sessions missing")
        original_pids = {pane: pid for pane, pid in self.remote_pids().items() if pane in expected}
        self.assertEqual(set(original_pids), expected)
        self.kitty("send-text", "--match", f"id:{bottom}", "echo New\\r")
        self.wait_for(lambda: "\nNew\n" in self.kitty("get-text", "--match", f"id:{bottom}", "--extent", "all"), "output missing")
        self.stop()
        self.assertTrue(expected.issubset(self.remote_sessions()))
        self.start(4)
        tree = self.tree()
        self.assertEqual({v[0] for v in pane_map(tree).values()}, expected)
        self.assertEqual({pane: pid for pane, pid in self.remote_pids().items() if pane in expected}, original_pids)
        self.assertEqual(focused_pane(tree), bottom_pane)
        tabs = [t for o in tree for t in o["tabs"]]
        self.assertEqual([len(t["windows"]) for t in tabs], [1, 3])
        bottom_window = next(w for t in tabs for w in t["windows"] if w["user_vars"]["kzw_pane"] == bottom_pane)
        left_pane = initial[second_tab_first][0]
        left_window = next(w for t in tabs for w in t["windows"] if w["user_vars"]["kzw_pane"] == left_pane)
        self.assertIn(bottom_window["id"], left_window["neighbors"]["bottom"])
        self.assertIn("\nNew\n", self.kitty("get-text", "--match", f'id:{bottom_window["id"]}', "--extent", "all"))
        right_window = next(w for t in tabs for w in t["windows"] if w["user_vars"]["kzw_pane"] == right_pane)
        self.command("close-pane", self.name, "--window-id", str(right_window["id"]))
        self.assertNotIn(right_pane, self.remote_sessions())
        self.stop()
        self.start(3)
        tree = self.tree()
        self.assertEqual({v[0] for v in pane_map(tree).values()}, expected - {right_pane})
        extra = int(self.kitty("launch", "--type=tab", "--tab-title=disposable"))
        self.kitty("launch", "--match", f"id:{extra}", "--next-to", f"id:{extra}", "--location=hsplit")
        self.wait_for(lambda: len(pane_map(self.tree())) == 5, "new tab metadata missing")
        tree = self.tree()
        tab = next(t for o in tree for t in o["tabs"] if t["title"] == "disposable")
        disposable = {w["user_vars"]["kzw_pane"] for w in tab["windows"]}
        self.wait_for(lambda: disposable.issubset(self.remote_sessions()), "tab sessions missing")
        self.stop()
        self.start(5)
        tree = self.tree()
        tab = next(t for o in tree for t in o["tabs"] if t["title"] == "disposable")
        self.assertEqual({w["user_vars"]["kzw_pane"] for w in tab["windows"]}, disposable)
        self.command("close-tab", self.name, "--tab-id", str(tab["id"]))
        self.assertTrue(disposable.isdisjoint(self.remote_sessions()))
        self.stop()
        self.start(3)
        self.assertEqual({v[0] for v in pane_map(self.tree()).values()}, expected - {right_pane})


if __name__ == "__main__":
    unittest.main()
