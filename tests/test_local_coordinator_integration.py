"""Opt-in isolated local coordinator and background Kitty lifecycle."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from kzw.core import pane_map


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real integration is opt-in")
class LocalCoordinatorIntegration(unittest.TestCase):
    def setUp(self):
        if any(not shutil.which(name) for name in ("kitty", "zmx", "kzw", "kzw-pane")):
            self.skipTest("requires kitty, zmx, kzw and kzw-pane on PATH")
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kc-", dir=cache)
        self.env = os.environ.copy()
        self.env.update(XDG_STATE_HOME=self.temp.name, ZMX_DIR=str(Path(self.temp.name) / "zmx"), SHELL="/bin/bash")
        self.env.pop("ZMX_SESSION", None)
        self.env.pop("ZMX_SESSION_PREFIX", None)
        # A remote-only override must not redirect or fence the local server.
        self.env["KZW_REMOTE_STATE_HOME"] = "/not-used-by-local-coordinator"
        if sys.platform == "darwin":
            self.env["KZW_TEST_KITTY_BACKGROUND"] = "1"
        self.path = Path(self.temp.name) / "kzw" / "Test"
        self.socket = self.path / "kitty.sock"
        self.process = None
        self.owned = set()
        self.log = Path(self.temp.name) / "supervisor.log"
        self.addCleanup(self.cleanup)

    def command(self, *args, input=None):
        return subprocess.run(["kzw", *args], env=self.env, input=input, text=True,
                              capture_output=True, timeout=60, check=True).stdout

    def kitty(self, *args):
        return subprocess.run(["kitty", "@", "--to", f"unix:{self.socket}", *args], env=self.env,
                              text=True, capture_output=True, timeout=10, check=True).stdout

    def tree(self):
        tree = json.loads(self.kitty("ls"))
        self.owned.update(p for p, _ in pane_map(tree).values())
        return tree

    def wait_for(self, predicate):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            time.sleep(0.1)
        self.fail("local coordinator workspace did not become ready:\n" +
                  (self.log.read_text() if self.log.exists() else ""))

    def attach(self, count, command="attach"):
        with self.log.open("a") as log:
            self.process = subprocess.Popen(["kzw", command, "Test"], env=self.env, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log)
        self.wait_for(lambda: len(pane_map(self.tree())) == count and
                      not (self.path / "pending-create.json").exists() and
                      set(json.loads((self.path / "panes.json").read_text())) ==
                      {p for p, _ in pane_map(self.tree()).values()})

    def detach(self):
        self.command("detach", "Test")
        self.process.wait(timeout=20)
        self.process = None

    def cleanup(self):
        safe = True
        if self.socket.exists():
            try:
                self.tree()
                self.kitty("close-window", "--match", "all")
            except (OSError, ValueError, subprocess.SubprocessError):
                safe = False
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                safe = False
        for pane in self.owned:
            try:
                listing = subprocess.run(["zmx", "list", "--short"], env=self.env, capture_output=True,
                                         text=True, timeout=10, check=True).stdout.splitlines()
                if pane in listing:
                    subprocess.run(["zmx", "kill", pane], env=self.env, capture_output=True,
                                   text=True, timeout=10, check=True)
            except (OSError, subprocess.SubprocessError):
                safe = False
        try:
            remaining = subprocess.run(["zmx", "list", "--short"], env=self.env, capture_output=True,
                                       text=True, timeout=10, check=True).stdout.splitlines()
            safe = safe and not remaining
            self.command("server-stop", input="y\n")
            socket = Path(self.temp.name) / "kzw" / "coordinator" / "server.sock"
            deadline = time.monotonic() + 5
            while socket.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            safe = safe and not socket.exists()
        except (OSError, subprocess.SubprocessError):
            safe = False
        if safe:
            self.temp.cleanup()
        else:
            self.temp._finalizer.detach()
            print(f"kzw local coordinator test: preserved state at {self.temp.name} for inspection")

    def test_local_coordinator_create_events_detach_and_reattach(self):
        self.command("create", "Test")
        self.assertIn("Test", self.command("list").splitlines())
        self.attach(1)
        initial = json.loads(self.command("inspect", "Test", "--json"))
        self.assertEqual(len(initial["panes"]), 1)
        wid, (pane, host) = next(iter(pane_map(self.tree()).items()))
        self.command("_managed-create", "Test", "right", str(wid), pane, "--host", host)
        current = json.loads(self.command("inspect", "Test", "--json"))
        self.assertEqual(len(current["panes"]), 2)
        events = [json.loads(line) for line in self.command("events", "Test", "--json", "--once",
                                                           "--after", str(initial["event_cursor"])).splitlines()]
        self.assertTrue(any(event["type"] == "layout.updated" and
                            set(event["data"]["panes"]) == set(current["panes"]) for event in events))
        self.kitty("launch", "--type=tab")
        def native_registered():
            panes = {p for p, _ in pane_map(self.tree()).values()}
            saved = json.loads(self.command("inspect", "Test", "--json"))
            return len(panes) == 3 and set(saved["panes"]) == panes and not saved["reservations"]
        self.wait_for(native_registered)
        current = json.loads(self.command("inspect", "Test", "--json"))
        panes = pane_map(self.tree())
        closing_id = next(wid for wid, (p, _) in panes.items() if p != pane)
        closing_pane = panes[closing_id][0]
        self.command("close-pane", "Test", "--window-id", str(closing_id), input="y\n")
        events = [json.loads(line) for line in self.command("events", "Test", "--json", "--once",
                                                           "--after", str(current["event_cursor"])).splitlines()]
        self.assertTrue(any(event["type"] == "pane.cleanup_completed" and
                            event["data"]["pane"] == closing_pane for event in events))
        current = json.loads(self.command("inspect", "Test", "--json"))
        self.assertEqual(len(current["panes"]), 2)
        self.detach()
        self.attach(2, command="start")
        self.assertEqual({p for p, _ in pane_map(self.tree()).values()}, set(current["panes"]))
        self.detach()

    def test_tab_title_tracks_focused_pane_foreground_process(self):
        # Exercise Kitty's own template compiler, not Python's str.format.
        program = ("import kitty.tab_bar as t; "
                   "c=t.compile_template(\"{title.replace('kzw-pane', '…')}\"); "
                   "print(eval(c, {}, {'title': 'kzw-pane'})); "
                   "print(eval(c, {}, {'title': 'bash'}))")
        rendered = subprocess.run(["kitty", "+runpy", program], env=self.env, text=True,
                                  capture_output=True, timeout=10, check=True).stdout.splitlines()
        self.assertEqual(rendered, ["…", "bash"])
        self.command("create", "Test")
        self.attach(1)
        first, (pane, host) = next(iter(pane_map(self.tree()).items()))
        self.command("_managed-create", "Test", "right", str(first), pane, "--host", host)
        self.wait_for(lambda: len(pane_map(self.tree())) == 2)
        second = next(wid for wid in pane_map(self.tree()) if wid != first)
        self.kitty("send-text", "--match", f"id:{first}", "sleep 60\n")

        def title_is(expected):
            return any(tab.get("title") == expected for window in self.tree() for tab in window["tabs"])

        self.kitty("focus-window", "--match", f"id:{first}")
        self.wait_for(lambda: title_is("sleep"))
        self.kitty("focus-window", "--match", f"id:{second}")
        self.wait_for(lambda: title_is("bash"))
        self.detach()
