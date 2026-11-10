import json
import subprocess
import unittest
from unittest.mock import patch

from kzw import appearance, cli


class AppearanceTests(unittest.TestCase):
    def test_capture_uses_source_window_and_validates_colors(self):
        payload = {"font_size": 19.5, "colors": "foreground #ffffff\nbackground #111111\nmark1_foreground #123456\ncolor255 #abcdef\n", "marker": ["function", "helpers/privacy_marker.py"]}
        result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch.dict("os.environ", {"KITTY_WINDOW_ID": "42"}, clear=True), \
             patch("kzw.appearance.subprocess.run", return_value=result) as run:
            options = appearance.capture()
        self.assertEqual(run.call_args.args[0][1:5], ["@", "kitten", "--match", "id:42"])
        self.assertIn("font_size=19.5", options["options"])
        self.assertIn("mark1_foreground=#123456", options["options"])
        self.assertEqual(options["marker"], ["function", "helpers/privacy_marker.py"])

    def test_marker_state_is_required_and_unsupported_markers_fail(self):
        base = {"font_size": 12, "colors": "foreground #ffffff\nbackground #111111\nmark1_foreground #123456\n"}
        for marker in ("missing", ["regex", [[1, "secret"]]]):
            payload = dict(base)
            if marker != "missing":
                payload["marker"] = marker
            with self.subTest(marker=marker), patch.dict("os.environ", {"KITTY_WINDOW_ID": "1"}, clear=True), \
                 patch("kzw.appearance.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")), \
                 self.assertRaises(ValueError):
                appearance.capture()

    def test_capture_fails_closed_for_missing_or_invalid_source(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(ValueError, "Kitty window"):
            appearance.capture()
        for payload in ({"font_size": float("nan"), "colors": ""},
                        {"font_size": 12, "colors": "shell #123456\n"},
                        {"font_size": 12, "colors": "foreground #ffffff\n"}):
            with self.subTest(payload=payload), patch.dict("os.environ", {"KITTY_WINDOW_ID": "1"}, clear=True), \
                 patch("kzw.appearance.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")), \
                 self.assertRaises(ValueError):
                appearance.capture()

    def test_attach_routes_inherited_appearance(self):
        with patch("sys.argv", ["kzw", "attach", "Test", "--host", "origin", "--inherit-appearance"]), \
             patch("kzw.appearance.capture", return_value=["-o", "font_size=20"]), \
             patch("kzw.cli.attach_remote", return_value=0) as attach:
            self.assertEqual(cli.main(), 0)
        attach.assert_called_once_with("Test", "origin", appearance=["-o", "font_size=20"])

    def test_create_rejects_inheritance_without_attach(self):
        with patch("sys.argv", ["kzw", "create", "Test", "--inherit-appearance"]), \
             patch("kzw.cli.create_remote") as create, patch("kzw.appearance.capture") as capture:
            self.assertEqual(cli.main(), 1)
        create.assert_not_called()
        capture.assert_not_called()
