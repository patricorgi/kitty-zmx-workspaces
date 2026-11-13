"""Opt-in launch probe: one disposable hidden view in the caller's Kitty.

No SSH, coordinator workspace or zmx session is used. Only the pane tagged with
this test's random token, matching its recorded PID/OS-window ID, may be closed.
"""

import importlib.util
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_shared_launch_test", REPO / "scripts/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real Kitty launch probe is opt-in")
class DemoSharedLaunchIntegrationTests(unittest.TestCase):
    def test_hidden_launch_uses_one_scoped_window_and_preserves_existing_panes(self):
        from kzw.kitty_instance import context, launch_command
        caller = context()
        kitty, sleep = shutil.which("kitty"), shutil.which("sleep")
        if not kitty or not sleep:
            self.skipTest("kitty and sleep are required")
        token = secrets.token_hex(12)

        def raw(*args):
            return subprocess.run([kitty, "@", "--to", caller["address"], *args], check=True,
                                  capture_output=True, text=True, timeout=15).stdout

        def panes(tree):
            return {(osw["id"], window["id"], window["pid"])
                    for osw in tree for tab in osw["tabs"] for window in tab["windows"]}

        def owned(tree):
            return [(osw, window) for osw in tree for tab in osw["tabs"] for window in tab["windows"]
                    if window.get("user_vars", {}).get("demo_probe") == token]

        before = json.loads(raw("ls"))
        baseline = panes(before)
        original_ids = {window["id"] for window in before}
        root = Path(tempfile.gettempdir())
        if (root / "opencode").is_dir():
            root /= "opencode"
        with tempfile.TemporaryDirectory(prefix="kzw-demo-launch-", dir=root) as directory:
            runner = object.__new__(demo.Demo)
            runner.kitty, runner.run_dir, runner.env = kitty, Path(directory), os.environ.copy()
            runner.write_launcher()
            session = runner.run_dir / "probe.session"
            session.write_text("new_tab Test\nlayout splits\nlaunch --var=demo_probe=" + token +
                               " --title=kzw-demo-launch-probe " + shlex.join([sleep, "120"]) + "\n")
            identity = None
            try:
                result = subprocess.run(launch_command(caller["group"], session), env=runner.env,
                                        capture_output=True, text=True, timeout=15)
                if result.returncode:
                    diagnostic = root / ("kzw-demo-launch-failure-" + token + ".log")
                    fd = os.open(diagnostic, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as log:
                        log.write(result.stdout + result.stderr)
                    self.fail("Kitty launcher failed; private diagnostics: " + str(diagnostic))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    tree = json.loads(raw("ls"))
                    matches = owned(tree)
                    if matches:
                        break
                    time.sleep(.1)
                else:
                    self.fail("the disposable tagged view did not appear")
                self.assertEqual(len(matches), 1)
                osw, window = matches[0]
                identity = (osw["id"], window["id"], window["pid"])
                self.assertEqual(window["cmdline"], [sleep, "120"])
                self.assertEqual({item["id"] for item in tree} - original_ids, {osw["id"]})
                self.assertTrue(baseline.issubset(panes(tree)))
                self.assertFalse(osw["is_focused"], "the hidden launch stole focus")
            finally:
                # Revalidate the tag, command, PID and both Kitty identities.
                matches = owned(json.loads(raw("ls")))
                for osw, window in matches:
                    current = (osw["id"], window["id"], window["pid"])
                    if (window["cmdline"] != [sleep, "120"] or osw["id"] in original_ids or
                            (identity is not None and current != identity)):
                        self.fail("disposable pane identity changed; left intact for inspection")
                    raw("close-window", "--match", "id:" + str(window["id"]))
                remaining = json.loads(raw("ls"))
                self.assertFalse(owned(remaining), "disposable pane was not cleaned up")
                self.assertTrue(baseline.issubset(panes(remaining)), "an existing pane identity changed")


if __name__ == "__main__":
    unittest.main()
