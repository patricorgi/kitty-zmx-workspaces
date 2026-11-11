import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kzw import watcher


class WatcherTests(unittest.TestCase):
    def test_events_notify_without_interpreting_close(self):
        with tempfile.TemporaryDirectory() as directory:
            signal = Path(directory) / "watcher.changed"
            with patch.dict(os.environ, {"KZW_CHANGE_SIGNAL": str(signal)}):
                for callback in (watcher.on_focus_change, watcher.on_resize,
                                 watcher.on_set_user_var, watcher.on_close):
                    with self.subTest(callback=callback.__name__):
                        signal.unlink(missing_ok=True)
                        callback(None, None, {})
                        self.assertTrue(signal.is_file())
                        self.assertEqual(list(Path(directory).iterdir()), [signal])

    def test_unavailable_signal_does_not_block_kitty(self):
        with patch.dict(os.environ, {"KZW_CHANGE_SIGNAL": "/nonexistent/kzw/signal"}):
            watcher.on_close(None, None, {})

    def test_command_events_only_record_valid_status(self):
        cases = (({"is_start": True}, "running"), ({"is_start": False, "exit_status": 0}, "exit:0"),
                 ({"is_start": False, "exit_status": 2}, "exit:2"),
                 ({"is_start": False, "exit_status": -9}, "exit:-9"),
                 ({"is_start": False, "exit_status": "bad"}, None),
                 ({"is_start": False, "exit_status": True}, None),
                 ({"is_start": False}, None), ({"is_start": 1}, None), ({}, None))
        for data, expected in cases:
            with self.subTest(data=data), patch.object(watcher, "_notify") as notify:
                window = Mock(user_vars={})
                watcher.on_cmd_startstop(None, window, data)
                if expected is None:
                    window.set_user_var.assert_not_called()
                    notify.assert_not_called()
                else:
                    window.set_user_var.assert_called_once_with("kzw_command_state", expected)
                    notify.assert_called_once_with(window)

    def test_shared_window_notifications_and_markers_use_only_selected_context(self):
        for context in ("explicit", "empty", "omitted"):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as tmp:
                own, stale = Path(tmp) / "own", Path(tmp) / "stale"
                marker = ["function", "own.py"]
                variables = {"kzw_workspace": "Test"}
                if context != "omitted":
                    variables.update(kzw_signal=str(own) if context == "explicit" else "",
                                     kzw_marker=json.dumps(marker) if context == "explicit" else "")
                window = types.SimpleNamespace(user_vars=variables, set_marker=Mock())
                with patch.dict(os.environ, {"KZW_CHANGE_SIGNAL": str(stale),
                                             "KZW_INHERIT_MARKER": '["function", "stale.py"]'}, clear=True):
                    watcher.on_focus_change(None, window, {})
                    watcher.on_resize(None, window, {})
                    watcher.on_set_user_var(None, window, {})
                    watcher.on_close(None, window, {})
                self.assertFalse(stale.exists())
                self.assertEqual(own.exists(), context == "explicit")
                if context == "explicit":
                    window.set_marker.assert_called_once_with(marker)
                else:
                    window.set_marker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
