import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from kzw import cli, supervisor_window


class SupervisorWindowTests(unittest.TestCase):
    def test_source_requires_interactive_macos_kitty(self):
        with patch.object(supervisor_window.sys, "platform", "darwin"), \
             patch.object(supervisor_window.sys.stdin, "isatty", return_value=True), \
             patch.dict(os.environ, {"KITTY_WINDOW_ID": "42"}, clear=True):
            self.assertEqual(supervisor_window.source_window(), "42")
            with patch.dict(os.environ, {"KZW_TEST_KITTY_BACKGROUND": "1"}):
                self.assertIsNone(supervisor_window.source_window())

    def test_change_targets_source_and_requires_ack(self):
        with patch("kzw.supervisor_window.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")) as run:
            supervisor_window.change("42", "minimize")
        self.assertEqual(run.call_args.args[0][1:5], ["@", "kitten", "--match", "id:42"])
        self.assertEqual(run.call_args.args[0][-1], "minimize")
        with patch("kzw.supervisor_window.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             self.assertRaises(ValueError):
            supervisor_window.change("42", "restore")

    def run_supervisor(self, capture_error=None, change_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "remote.json").write_text("{}")
            child = Mock(returncode=0)
            child.poll.side_effect = [None, 0]
            title_stop = Mock()
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.prepare_bootstrap", return_value=path / "bootstrap.kitty-session"), \
                 patch("kzw.cli.mark_bootstrap_launching"), patch("kzw.cli.finish_bootstrap"), \
                 patch("kzw.cli.start_title_monitor", return_value=title_stop), \
                 patch("kzw.cli.remote_client.start_status_monitor", return_value=lambda: None), \
                 patch("kzw.cli.remote_client.ConnectionBroker") as broker, \
                 patch("kzw.cli.subprocess.Popen", return_value=child), \
                 patch("kzw.cli.synchronize_exits"), patch("kzw.cli.capture", side_effect=capture_error), \
                 patch("kzw.cli.supervisor_window.source_window", return_value="42"), \
                 patch("kzw.cli.supervisor_window.change", side_effect=change_error) as change, \
                 patch("kzw.cli.time.sleep"):
                broker.return_value.path = path / "rpc.sock"
                self.assertEqual(cli.run("Test", "origin", remote_attached=True, new_instance=True), 0)
                return change.call_args_list

    def test_minimizes_only_after_capture_and_restores(self):
        self.assertEqual(self.run_supervisor(), [call("42", "minimize"), call("42", "restore")])

    def test_failed_capture_never_minimizes(self):
        self.assertEqual(self.run_supervisor(capture_error=ValueError("not ready")), [])

    def test_lost_minimize_reply_still_attempts_restore(self):
        self.assertEqual(self.run_supervisor(change_error=[OSError("lost reply"), None]),
                         [call("42", "minimize"), call("42", "restore")])
