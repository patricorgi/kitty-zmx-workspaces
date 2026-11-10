"""Opt-in hidden Kitty-to-Kitty live appearance transfer."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from kzw import appearance


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "real integration is opt-in")
class AppearanceIntegration(unittest.TestCase):
    def test_hidden_instances_copy_live_colors_and_font_size(self):
        if not shutil.which("kitty"):
            self.skipTest("kitty is required")
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ka-", dir=cache) as temporary:
            processes = []
            def command(socket, *args):
                return subprocess.run(["kitty", "@", "--to", "unix:" + str(socket), *args],
                                      text=True, capture_output=True, timeout=10, check=True).stdout

            def launch(name, options=(), env=None):
                socket = Path(temporary) / (name + ".sock")
                proc = subprocess.Popen(["kitty", "--start-as=hidden", "--listen-on", "unix:" + str(socket),
                                         "-c", "NONE", "-o", "allow_remote_control=socket-only",
                                         *options, "/bin/sh"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                processes.append((proc, socket))
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        tree = json.loads(command(socket, "ls"))
                        windows = [w for osw in tree for tab in osw["tabs"] for w in tab["windows"]]
                        if len(windows) == 1:
                            return socket, windows[0]["id"]
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    time.sleep(0.1)
                self.fail("hidden Kitty did not become ready")

            def capture(socket, window):
                with patch.dict(os.environ, {"KITTY_WINDOW_ID": str(window), "KITTY_LISTEN_ON": "unix:" + str(socket)}):
                    return appearance.capture()

            try:
                source, source_window = launch("source")
                marker = Path(temporary) / "marker.py"
                marker.write_text("def marker(text):\n    return [(0, 1, 1)] if text else []\n")
                command(source, "set-font-size", "19.5")
                command(source, "set-colors", "--all", "--configured", "mark1_foreground=#123456")
                command(source, "set-colors", "--match", "id:" + str(source_window), "foreground=#abcdef")
                self.assertIsNone(capture(source, source_window)["marker"])
                command(source, "create-marker", "--match", "id:" + str(source_window), "function", str(marker))
                captured = capture(source, source_window)
                options = captured["options"]
                self.assertEqual(captured["marker"], ["function", str(marker)])
                self.assertIn("font_size=19.5", options)
                self.assertIn("mark1_foreground=#123456", options)
                self.assertIn("foreground=#abcdef", options)
                env = os.environ.copy()
                env["KZW_INHERIT_MARKER"] = json.dumps(captured["marker"])
                watcher = Path(__file__).resolve().parents[1] / "kzw" / "watcher.py"
                target, target_window = launch("target", [*options, "-o", "watcher=" + str(watcher)], env)
                copied = capture(target, target_window)
                self.assertEqual(copied["marker"], ["function", str(marker)])
                self.assertIn("font_size=19.5", copied["options"])
                self.assertIn("mark1_foreground=#123456", copied["options"])
                second = int(command(target, "launch", "--type=window", "/bin/sh").strip())
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if capture(target, second)["marker"] == ["function", str(marker)]:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("new pane did not inherit the marker")
                command(target, "action", "--match", "id:" + str(second), "remove_marker")
                command(target, "set-font-size", "20")
                self.assertIsNone(capture(target, second)["marker"])
            finally:
                for proc, socket in reversed(processes):
                    if proc.poll() is None:
                        try:
                            command(socket, "close-window", "--match", "all")
                            proc.wait(timeout=10)
                        except (OSError, subprocess.SubprocessError):
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=5)
