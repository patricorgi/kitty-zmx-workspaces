"""Optional differential checks against the existing Python implementation."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import unittest

from kzw.core import pane_map, rewrite_export

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "rust" / "target" / "debug" / "kzw-snapshot"
PANE = "kzw-" + "a" * 32


@unittest.skipUnless(os.environ.get("KZW_VERIFY_RUST_PARITY") == "1", "Rust parity checks are opt-in")
class RustSnapshotParity(unittest.TestCase):
    def transform(self, tree, export, executable):
        return subprocess.run([str(BINARY)], input=json.dumps({"tree": tree, "export": export,
                                                              "executable": executable}),
                              text=True, capture_output=True, timeout=10)

    def test_valid_snapshots_match_python_semantically(self):
        tree = [{"tabs": [{"windows": [{"id": 7, "user_vars": {"kzw_pane": PANE, "kzw_host": "origin"}}]}]}]
        for title in ("plain", "spaces and 'quotes'", "unicode π"):
            with self.subTest(title=title):
                data = json.dumps({"id": 7, "title": title, "cmd_at_shell_startup": "do not run"})
                export = "new_tab Test\n" + shlex.join(["launch", "kitty-unserialize-data=" + data,
                                                            "--var=kzw_staging=1", "--cwd=/a b", "echo", "unsafe"]) + "\n"
                executable = "/path with 'quotes'/kzw-pane"
                expected = rewrite_export(export, pane_map(tree), executable)
                result = self.transform(tree, export, executable)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = json.loads(result.stdout)["snapshot"]
                expected_args = shlex.split(expected.splitlines()[1])
                actual_args = shlex.split(actual.splitlines()[1])
                self.assertEqual(actual_args[0], expected_args[0])
                self.assertEqual(json.loads(actual_args[1].split("=", 1)[1]),
                                 json.loads(expected_args[1].split("=", 1)[1]))
                self.assertEqual(actual_args[2:], expected_args[2:])

    def test_both_reject_partial_and_unmanaged_layouts(self):
        tree = [{"tabs": [{"windows": [{"id": 7, "user_vars": {"kzw_pane": PANE}}]}]}]
        for export in ("new_tab Test\n", "launch --cwd=/tmp bash\n",
                       "launch 'kitty-unserialize-data={\"id\":8}'\n"):
            with self.subTest(export=export):
                with self.assertRaises(ValueError):
                    rewrite_export(export, pane_map(tree), "/bin/kzw-pane")
                self.assertNotEqual(self.transform(tree, export, "/bin/kzw-pane").returncode, 0)
