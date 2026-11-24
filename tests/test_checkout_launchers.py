"""The checkout launchers must not require an installed virtual environment."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CheckoutLauncherTests(unittest.TestCase):
    def test_launchers_work_outside_the_checkout_without_pythonpath(self):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("VIRTUAL_ENV", None)
        env["PATH"] = str(Path(sys.executable).resolve().parent) + os.pathsep + "/usr/bin:/bin"
        with tempfile.TemporaryDirectory() as directory:
            for args in ([], ["pane"]):
                with self.subTest(args=args):
                    result = subprocess.run([str(ROOT / "bin/kzw"), *args, "--help"], cwd=directory,
                                            env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout)
