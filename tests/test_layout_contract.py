"""Server validation must reject bad joins without touching authoritative state."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kzw.remote import Store, layout_sessions
from workspace_fixtures import layout, split_layout

A, B, C = ("kzw-" + letter * 32 for letter in "abc")


def with_state(snapshot, change):
    lines = snapshot.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("set_layout_state "))
    state = json.loads(lines[index].split(" ", 1)[1])
    change(state)
    lines[index] = "set_layout_state " + json.dumps(state)
    return "\n".join(lines) + "\n"


class LayoutContractTests(unittest.TestCase):
    def test_supported_layouts_preserve_order_focus_sizes_and_session_identity(self):
        original = split_layout(A, B, C)
        cases = (
            ("split tabs", original, [A, B, C], {A: (1, 1, 17, False), B: (1, 2, 91, True), C: (2, 1, 33, False)}),
            ("ordered panes", layout(B, A), [A, B], {B: (1, 1, 1, False), A: (1, 2, 2, False)}),
            ("empty", "# kzw-empty-workspace\n", [], {}),
            ("dormant", "# kzw-dormant-workspace\n", [], {}),
            ("non-split layout", original.replace('"class": "Splits"', '"class": "Tall"'), [A, B, C],
             {A: (1, 1, 17, False), B: (1, 2, 91, True), C: (2, 1, 33, False)}),
        )
        for name, snapshot, manifest, expected in cases:
            with self.subTest(case=name):
                before = copy.deepcopy(manifest)
                result = layout_sessions(snapshot, manifest)
                self.assertEqual({pane: (loc["tab"], loc["pane"], loc["serialized_window_id"], loc["focused"])
                                  for pane, loc in result.items()}, expected)
                self.assertEqual(manifest, before)
        # The server must retain the full representation, including split sizes.
        with tempfile.TemporaryDirectory() as tmp, patch("kzw.remote.subprocess.run", return_value=
                subprocess.CompletedProcess([], 0, "\n".join([A, B, C]) + "\n", "")):
            store = Store(Path(tmp) / "server.sqlite")
            try:
                store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 0,
                                "request_id": "layout", "snapshot": original, "panes": [A, B, C]})
                self.assertEqual(store.dispatch({"method": "workspace.get", "workspace": "Test"})["snapshot"], original)
            finally:
                store.close()

    def test_invalid_manifests_launches_focus_and_topologies_fail_without_commit(self):
        valid = split_layout(A, B, C)
        cases = [
            ("no panes", "", []), ("wrong manifest type", valid, {}),
            ("duplicate manifest", valid, [A, A, C]), ("invalid ID", valid, [A, B, "other"]),
            ("missing leaf", valid, [A, B]), ("empty with panes", "# kzw-empty-workspace\n", [A]),
            ("unmanaged command", valid.replace("KZW_PANE_EXECUTABLE", "/bin/sh"), [A, B, C]),
            ("duplicate launch", valid + layout(A), [A, B, C]),
            ("startup command", valid.replace('"id": 17', '"id": 17, "cmd_at_shell_startup": "rm"'), [A, B, C]),
            ("boolean window ID", valid.replace('"id": 17', '"id": true'), [A, B, C]),
            ("missing serialized data", valid.replace("kitty-unserialize-data=", "other="), [A, B, C]),
            ("mismatched variable", valid.replace("--var=kzw_pane=" + A, "--var=kzw_pane=" + B), [A, B, C]),
            ("foreign variable", valid.replace("--var=kzw_host=KZW_ORIGIN", "--var=kzw_host=foreign"), [A, B, C]),
            ("extra program", valid.replace("KZW_PANE_EXECUTABLE", "other KZW_PANE_EXECUTABLE"), [A, B, C]),
            ("no first tab", valid.replace("new_tab Test\n", ""), [A, B, C]),
            ("empty middle tab", valid.replace("new_tab Second", "new_tab Empty\nnew_tab Second"), [A, B, C]),
            ("empty final tab", valid + "new_tab Empty\n", [A, B, C]),
            ("two OS windows", valid + "new_os_window\n", [A, B, C]),
            ("foreign focus", valid.replace('"pane": "' + B, '"pane": "kzw-' + 'f' * 32), [A, B, C]),
            ("duplicate focus", valid + '# kzw-focus ' + json.dumps({"pane": A}) + '\n', [A, B, C]),
            ("malformed focus", valid.replace('{"pane": "' + B + '"}', '[]'), [A, B, C]),
            ("state without tab", "set_layout_state {}\n" + valid, [A, B, C]),
            ("missing state", valid + "set_layout_state\n", [A, B, C]),
        ]
        mutations = {
            "invalid groups": lambda s: s.update(all_windows=[]),
            "empty groups": lambda s: s["all_windows"].update(window_groups=[]),
            "empty group": lambda s: s["all_windows"]["window_groups"][0].update(window_ids=[]),
            "duplicate group": lambda s: s["all_windows"]["window_groups"][1].update(id=7),
            "duplicate window": lambda s: s["all_windows"]["window_groups"][1].update(window_ids=[17]),
            "foreign window": lambda s: s["all_windows"]["window_groups"][1].update(window_ids=[999]),
            "boolean group": lambda s: s["all_windows"]["window_groups"][0].update(id=True),
            "missing split leaf": lambda s: s.update(pairs={"one": 7}),
            "foreign split leaf": lambda s: s["pairs"].update(two=999),
            "duplicate split leaf": lambda s: s["pairs"].update(two=7),
            "invalid split branch": lambda s: s["pairs"].update(two={"two": 8}),
        }
        cases += [(name, with_state(valid, mutate), [A, B, C]) for name, mutate in mutations.items()]
        with tempfile.TemporaryDirectory() as tmp, patch("kzw.remote.subprocess.run") as run:
            store = Store(Path(tmp) / "server.sqlite")
            try:
                for index, (name, snapshot, manifest) in enumerate(cases):
                    with self.subTest(case=name):
                        with self.assertRaises(ValueError):
                            store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 0,
                                            "request_id": str(index), "snapshot": snapshot, "panes": manifest})
                        self.assertFalse(store.dispatch({"method": "workspace.get", "workspace": "Test"})["exists"])
                        self.assertEqual(store.dispatch({"method": "workspace.events", "workspace": "Test"})["events"], [])
                        self.assertEqual(store.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)
                        self.assertFalse(store.db.in_transaction)
                run.assert_not_called()
            finally:
                store.close()
