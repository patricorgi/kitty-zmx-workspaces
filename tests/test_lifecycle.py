"""Deterministic lifecycle scenarios using simulated Kitty and zmx interfaces.

These tests exercise the coordinator's real snapshot and close code. The fake
does not claim to emulate Kitty, SSH, zmx, or rendering: real-system coverage
is in the separately opt-in test_integration.py.
"""

import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kzw.cli import capture, close_pane, close_tab, detach, restore_focus
from kzw.core import focused_pane, pane_map, saved_focus


def identity(n):
    return "kzw-" + f"{n:032x}"


class FakeKitty:
    def __init__(self):
        self.tabs = [self.tab(1, [(1, 1)]), self.tab(2, [(2, 2), (3, 3)])]
        self.active = 2
        self.focused = 3
        self.sessions = {identity(i): {"output": "", "pid": i * 100} for i in (1, 2, 3)}
        self.attached = True
        self.fail_close = False

    @staticmethod
    def tab(tab_id, windows):
        return {"id": tab_id, "windows": [{"id": wid, "pane": identity(pid)} for wid, pid in windows]}

    def add(self, tab_id, window_id, pane_number):
        tab = next(t for t in self.tabs if t["id"] == tab_id)
        tab["windows"].append({"id": window_id, "pane": identity(pane_number)})
        self.sessions[identity(pane_number)] = {"output": "", "pid": pane_number * 100}
        self.active, self.focused = tab_id, window_id

    def tree(self):
        return [{"last_focused": True, "tabs": [{"id": tab["id"], "is_active": tab["id"] == self.active,
                 "windows": [{"id": w["id"], "is_active": w["id"] == self.focused,
                              "user_vars": {"kzw_pane": w["pane"], "kzw_host": "fake-host"}}
                             for w in tab["windows"]]} for tab in self.tabs]}]

    def export(self):
        lines = []
        for tab in self.tabs:
            lines += [f'new_tab tab-{tab["id"]}', 'layout splits']
            # This marker stands for Kitty's serialized tree; it includes
            # arrangement and identity to catch accidental loss in rewriting.
            lines.append('set_layout_state ' + json.dumps({"pairs": [w["id"] for w in tab["windows"]]}))
            for w in tab["windows"]:
                lines.append(shlex.join(["launch", 'kitty-unserialize-data=' + json.dumps({"id": w["id"]})]))
        return "\n".join(lines) + "\n"

    def query(self, socket, *args):
        if args == ("ls",):
            return json.dumps(self.tree())
        if args == ("ls", "--output-format=session"):
            return self.export()
        if args[:2] == ("focus-window", "--match"):
            self.focused = int(args[2].split(":")[1])
            self.active = next(t["id"] for t in self.tabs if any(w["id"] == self.focused for w in t["windows"]))
            return ""
        if args == ("close-window", "--match", "all"):
            self.attached = False
            return ""
        if args[0] in ("close-window", "close-tab"):
            if self.fail_close:
                raise OSError("simulated Kitty failure")
            target = int(args[2].split(":")[1])
            if args[0] == "close-tab":
                self.tabs = [t for t in self.tabs if t["id"] != target]
            else:
                for tab in self.tabs:
                    tab["windows"] = [w for w in tab["windows"] if w["id"] != target]
                self.tabs = [t for t in self.tabs if t["windows"]]
            self.active = self.tabs[0]["id"]
            self.focused = self.tabs[0]["windows"][0]["id"]
            return ""
        raise AssertionError(f"unexpected Kitty call: {args}")

    def zmx(self, host, *args):
        if args == ("list", "--short"):
            return "\n".join(self.sessions) + "\n"
        if args[0] == "kill":
            del self.sessions[args[1]]
            return ""
        raise AssertionError(f"unexpected zmx call: {args}")


class LifecycleScenarios(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.fake = FakeKitty()
        self.patchers = [patch("kzw.cli.state_dir", return_value=self.path),
                         patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"),
                         patch("kzw.cli.query", side_effect=self.fake.query),
                         patch("kzw.cli.zmx_command", side_effect=self.fake.zmx)]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(self.temp.cleanup)
        for patcher in self.patchers:
            self.addCleanup(patcher.stop)

    def assert_mapping(self):
        panes = {pane for pane, _ in pane_map(self.fake.tree()).values()}
        self.assertEqual(panes, set(self.fake.sessions))

    def test_detach_snapshot_retains_focus_and_unique_sessions(self):
        original = dict(self.fake.sessions)
        detach("Test")
        self.assertFalse(self.fake.attached)
        self.assertEqual(self.fake.sessions, original)
        text = (self.path / "workspace.kitty-session").read_text()
        self.assertEqual(saved_focus(text), identity(3))
        self.assertEqual(set(json.loads((self.path / "panes.json").read_text())), set(original))
        self.assertIn('set_layout_state {"pairs": [2, 3]}', text)
        self.fake.attached = True
        self.fake.focused, self.fake.active = 1, 1
        restore_focus(self.path / "kitty.sock", saved_focus(text))
        self.assertEqual(focused_pane(self.fake.tree()), identity(3))
        self.assert_mapping()

    def test_add_bottom_split_save_then_close_right_pane(self):
        # Simulate Kitty creating a managed pane; snapshots must include its
        # identity, while its independently owned output persists across detach.
        self.fake.add(2, 4, 4)
        self.fake.sessions[identity(4)]["output"] = "New\n"
        capture(self.path, "/bin/kzw-pane")
        self.assertIn('set_layout_state {"pairs": [2, 3, 4]}', (self.path / "workspace.kitty-session").read_text())
        detach("Test")
        self.fake.attached = True
        self.assertEqual(self.fake.sessions[identity(4)]["output"], "New\n")
        close_pane("Test", 3)
        self.assertNotIn(identity(3), self.fake.sessions)
        self.assertIn(identity(4), self.fake.sessions)
        self.assert_mapping()
        detach("Test")
        self.assertNotIn(identity(3), (self.path / "workspace.kitty-session").read_text())

    def test_new_two_pane_tab_then_close_only_that_tab(self):
        self.fake.tabs.append(self.fake.tab(3, []))
        self.fake.add(3, 4, 4)
        self.fake.add(3, 5, 5)
        detach("Test")
        self.fake.attached = True
        close_tab("Test", 3)
        self.assertEqual(set(self.fake.sessions), {identity(1), identity(2), identity(3)})
        self.assertEqual([t["id"] for t in self.fake.tabs], [1, 2])
        self.assert_mapping()
        detach("Test")
        self.assertNotIn("new_tab tab-3", (self.path / "workspace.kitty-session").read_text())

    def test_failed_kitty_close_never_kills_sessions(self):
        self.fake.fail_close = True
        with self.assertRaises(OSError):
            close_pane("Test", 3)
        self.assertEqual(set(self.fake.sessions), {identity(1), identity(2), identity(3)})
        self.assertEqual(json.loads((self.path / "pending-close.json").read_text())["phase"], "closing")


if __name__ == "__main__":
    unittest.main()
