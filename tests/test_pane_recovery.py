import io
import os
import pty
import subprocess
import termios
import tty
import unittest
from unittest.mock import patch

from kzw import pane, remote_client


PANE = "kzw-" + "a" * 32


class PaneRecoveryTests(unittest.TestCase):
    def test_disconnected_terminal_restores_input_and_disables_event_reporting(self):
        class TerminalOutput(io.StringIO):
            def isatty(self):
                return True

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before = termios.tcgetattr(slave)
        for failure in (False, True):
            with self.subTest(failure=failure), os.fdopen(os.dup(slave)) as stdin, \
                 patch("sys.stdin", stdin), patch("sys.stdout", new_callable=TerminalOutput) as stdout:
                try:
                    with pane.attachment_terminal():
                        tty.setraw(slave)
                        if failure:
                            raise OSError("SSH disconnected")
                except OSError:
                    pass
                restored = termios.tcgetattr(slave)
                # Darwin can mark input for reprocessing after tcsetattr.
                restored[3] &= ~getattr(termios, "PENDIN", 0)
                expected = before.copy()
                expected[3] &= ~getattr(termios, "PENDIN", 0)
                self.assertEqual(restored, expected)
                modes = stdout.getvalue()
                self.assertTrue(modes.startswith("\x1b[?") and modes.endswith("l"))
                self.assertIn("1004", modes)  # focus reporting
                self.assertIn("1003", modes)  # all mouse movement
                self.assertIn("1006", modes)  # SGR mouse coordinates
                self.assertNotIn("\x1b[2J", modes)

    def test_local_and_remote_panes_wait_for_lease_without_error_spam_or_new_sessions(self):
        for host in ("", "origin"):
            with self.subTest(host=host):
                env = {"KZW_WORKSPACE_COORDINATOR": "1", "KZW_WORKSPACE_ORIGIN": host,
                       "KZW_WORKSPACE_DIRECTORY": "/private/Test", "KZW_WORKSPACE": "Test"}
                errors = [remote_client.RemoteRejected("lease expired"), remote_client.RemoteRejected("lease expired"), None]
                with patch.dict(os.environ, env, clear=True), \
                     patch("sys.argv", ["kzw-pane", "--pane", PANE, "--host", host, "--existing"]), \
                     patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                     patch("kzw.pane.verify_workspace_lease", side_effect=errors), \
                     patch("kzw.pane.run_attachment", return_value=subprocess.CompletedProcess([], 0)) as attach, \
                     patch("kzw.pane.subprocess.run", return_value=subprocess.CompletedProcess([], 0, PANE + "\n")), \
                     patch("kzw.pane.report_attachment_ended") as ended, \
                     patch("kzw.pane.set_attachment_ended") as marker:
                    pane.main()
                self.assertEqual(stderr.getvalue().count("lease expired"), 1)
                self.assertEqual([c.kwargs["retry"] for c in ended.call_args_list], [True, True, False])
                marker.assert_called_once_with(False)
                attach.assert_called_once()
                command = attach.call_args.args[0]
                if host:
                    self.assertIn("zmx list --short", command[-1])
                    self.assertIn("exec zmx attach " + PANE, command[-1])
                else:
                    self.assertEqual(command, ["zmx", "attach", PANE])
