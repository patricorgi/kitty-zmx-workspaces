"""Opt-in native Kitty tab reorder followed immediately by detach/reattach."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from kzw import cli, sessions
import test_cwd_integration as cwd_tests


def pane_id(window):
    return window["user_vars"]["kzw_pane"]


def tab_layout(tab):
    """Compare split geometry using persistent panes rather than Kitty group IDs."""
    by_window = {w["id"]: pane_id(w) for w in tab["windows"]}
    layout = tab["layout_state"]
    groups = {g["id"]: tuple(by_window[i] for i in g["window_ids"])
              for g in layout["all_windows"]["window_groups"]}

    def pair(node):
        if isinstance(node, int):
            return groups[node]
        return {k: pair(v) if k in ("one", "two") else v for k, v in node.items()}

    topology = pair(layout["pairs"]) if "pairs" in layout else tuple(by_window.values())
    return tab["layout"], topology, next(pane_id(w) for w in tab["windows"] if w["is_active"])


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real Kitty is opt-in")
class TabOrderIntegrationTests(unittest.TestCase):
    def check_reorder(self, host):
        mode = "remote" if host else "local"
        fixture = cwd_tests.CwdIntegrationTests(f"test_{mode}_tabs_and_splits_inherit_selected_shell_cwd")
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": config.name}):
            fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups(), "tab reorder fixture cleanup failed"))
        kit = fixture.kitty
        origin = ["--host", host] if host else []

        def command(*args):
            result = kit.kzw(*args, *origin)
            self.assertEqual(result.returncode, 0, result.stderr)

        command("create", "Test", "--attach")
        source, = fixture.wait(fixture.windows)
        with patch.dict(os.environ, kit.env):
            directory = cli.state_dir("Test", host)

        def tabs():
            os_id = cli.view_state(directory)["os_window_id"]
            return next(ow["tabs"] for ow in json.loads(kit.raw("ls")) if ow["id"] == os_id)

        # A, B, C; split C and move it with the second split focused.
        for count, kind in enumerate(("tab", "tab", "right"), 2):
            if kind == "right":
                kit.raw("goto-layout", "--match", f"window_id:{source['id']}", "splits")
            kit.raw("kitten", "--match", f"id:{source['id']}", str(cwd_tests.shared.REPO / "kzw/create_kitten.py"), kind)
            rows = fixture.wait(lambda: rows if len(rows := fixture.windows()) == count
                                and not (directory / "pending-create.json").exists() else None)
            source = max(rows, key=lambda w: w["id"])
        original = tabs()
        order = [[pane_id(w) for w in tab["windows"]] for tab in original]
        self.assertEqual([len(tab) for tab in order], [1, 1, 2])
        expected = [order[2], order[0], order[1]]
        match = f"id:{source['id']}"
        kit.raw("focus-window", "--match", match)
        neighbors = source["neighbors"]
        axis = "horizontal" if neighbors.get("left") or neighbors.get("right") else "vertical"
        kit.raw("resize-window", "--match", match, "--axis", axis, "--increment", "4")
        layouts = [tab_layout(tab) for tab in tabs()]
        self.assertEqual(layouts[2][0], "splits")
        wanted = {p for tab in order for p in tab}

        def identities():
            with patch.dict(os.environ, kit.env):
                text = cli.zmx_command(host, "list")
            rows = [dict(field.split("=", 1) for field in line.strip().split("\t")) for line in text.splitlines()]
            return {r["name"]: (r["pid"], r["created"]) for r in rows if r["name"] in wanted}

        before = identities()
        self.assertEqual(set(before), wanted)
        kit.raw("action", "--match", match, "move_tab_backward")
        kit.raw("action", "--match", match, "move_tab_backward")
        command("detach", "Test")  # No wait for an autosave between move and detach.
        with patch.dict(os.environ, kit.env):
            saved = cli.remote_snapshot("Test", host)
        self.assertEqual(sessions.snapshot_tabs(saved["snapshot"], saved["panes"]), expected)
        self.assertEqual(saved["focused_pane"], pane_id(source))

        command("attach", "Test")
        fixture.wait(lambda: cli.view_state(directory) and len(fixture.windows()) == len(wanted))
        restored = tabs()
        self.assertEqual([tab_layout(tab) for tab in restored], [layouts[2], layouts[0], layouts[1]])
        self.assertTrue(restored[0]["is_active"])
        self.assertEqual(identities(), before)

    def test_local_reorder(self):
        self.check_reorder("")

    @unittest.skipUnless(os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "real SSH is opt-in")
    def test_remote_reorder(self):
        self.check_reorder(os.environ["KZW_INTEGRATION_HOST"])
