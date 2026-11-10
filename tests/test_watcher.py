import os
import tempfile
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
        window = Mock()
        with patch.dict(os.environ, {}, clear=True):
            watcher.on_cmd_startstop(None, window, {"is_start": True})
            watcher.on_cmd_startstop(None, window, {"is_start": False, "exit_status": 2})
            watcher.on_cmd_startstop(None, window, {"is_start": False, "exit_status": "bad"})
        self.assertEqual(window.set_user_var.call_args_list[0].args, ("kzw_command_state", "running"))
        self.assertEqual(window.set_user_var.call_args_list[1].args, ("kzw_command_state", "exit:2"))
        self.assertEqual(window.set_user_var.call_count, 2)


if __name__ == "__main__":
    unittest.main()
