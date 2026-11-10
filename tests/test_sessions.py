import io
import json
import os
import pty
import shlex
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from kzw import sessions

A = "kzw-" + "a" * 32
B = "kzw-" + "b" * 32
C = "kzw-" + "c" * 32


def launch(pane):
    return shlex.join(["launch", 'kitty-unserialize-data={"id":1}', "KZW_PANE_EXECUTABLE",
                       "--pane", pane, "--host", "KZW_ORIGIN", "--existing"])


LAYOUT = "\n".join(["new_tab first", launch(A), launch(B), "new_tab second", launch(C)]) + "\n"


class SessionTests(unittest.TestCase):
    def test_host_local_snapshot_uses_existing_private_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.join(temporary, "kzw")
            os.mkdir(root, 0o700)
            path = os.path.join(root, "server.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(1)
            server.settimeout(5)
            requests = []
            def handle():
                client, _ = server.accept()
                with client, client.makefile("rb") as stream:
                    requests.append(json.loads(stream.readline()))
                    client.sendall(json.dumps({"ok": True, "result": {"exists": True, "revision": 1,
                                                                     "snapshot": LAYOUT, "panes": [A, B, C]}}).encode() + b"\n")
            thread = threading.Thread(target=handle)
            thread.start()
            try:
                with patch.dict(os.environ, {"XDG_STATE_HOME": temporary}):
                    state = sessions.coordinator_snapshot("Test")
                self.assertEqual(state["revision"], 1)
                self.assertEqual(requests, [{"method": "workspace.sync", "workspace": "Test"}])
            finally:
                thread.join(timeout=5)
                server.close()

    def test_layout_preserves_tab_and_pane_order(self):
        self.assertEqual(sessions.snapshot_tabs(LAYOUT, [C, A, B]), [[A, B], [C]])
        self.assertEqual(sessions.snapshot_tabs("# kzw-dormant-workspace\n", []), [])

    def test_layout_refuses_missing_duplicate_or_unmanaged_panes(self):
        for layout, manifest in ((LAYOUT, [A, B]), (LAYOUT + launch(A) + "\n", [A, B, C]),
                                 (LAYOUT.replace("KZW_ORIGIN", "other"), [A, B, C]),
                                 ("new_tab a\nlaunch /bin/sh\n", [A])):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                sessions.snapshot_tabs(layout, manifest)

    def test_selector_skips_unavailable_and_selects_only_panes(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        output = io.StringIO()
        output.isatty = lambda: True
        with os.fdopen(os.dup(slave), "r") as stream:
            def send():
                time.sleep(0.1)
                os.write(master, b"\x1b[B\r")
            thread = threading.Thread(target=send)
            thread.start()
            self.assertEqual(sessions.choose("Test", [[A, B], [C]], {A, C}, stream, output), C)
            thread.join()
        self.assertIn("Split 2 — unknown [unavailable]", output.getvalue())
        self.assertNotIn(B, output.getvalue())

    def test_number_attaches_live_pane_immediately(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        output = io.StringIO()
        output.isatty = lambda: True
        with os.fdopen(os.dup(slave), "r") as stream:
            def send():
                time.sleep(0.1)
                os.write(master, b"1")
            thread = threading.Thread(target=send)
            thread.start()
            self.assertEqual(sessions.choose("Test", [[A, B], [C]], {A, C}, stream, output), C)
            thread.join()
        self.assertIn("[0] Split 1", output.getvalue())
        self.assertIn("[1] Split 1", output.getvalue())
        self.assertIn("[ ] Split 2 — unknown [unavailable]", output.getvalue())

    def test_unassigned_number_does_not_select_unavailable_pane(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        output = io.StringIO()
        output.isatty = lambda: True
        with os.fdopen(os.dup(slave), "r") as stream:
            def send():
                time.sleep(0.1)
                os.write(master, b"9q")
            thread = threading.Thread(target=send)
            thread.start()
            self.assertIsNone(sessions.choose("Test", [[A, B]], {A}, stream, output))
            thread.join()

    def test_process_labels_do_not_guess_bash_history(self):
        processes = {A: {"name": "bash", "pid": 101}, B: {"name": "nvim", "pid": 202},
                     C: {"name": "bash", "pid": 303}}
        with patch.object(sessions, "read_process_details", return_value=processes), \
             patch.object(sessions.session_meta, "read", side_effect=lambda pane, pid: "git status" if pane == A else None):
            self.assertEqual(sessions.process_labels(), {A: "bash: git status", B: "nvim", C: "bash: unavailable"})

    def test_failed_process_lookup_does_not_block_selector(self):
        with patch.object(sessions, "read_process_details", side_effect=OSError("unavailable")):
            self.assertEqual(sessions.process_labels(), {})

    def test_attachment_rechecks_revision_and_session(self):
        state = {"exists": True, "revision": 1, "snapshot": LAYOUT, "panes": [A, B, C]}
        with patch.object(sessions, "coordinator_snapshot", return_value=state), \
             patch.object(sessions, "choose", return_value=B), \
             patch.object(sessions, "live_sessions", side_effect=[{A, B}, {A, B}]), \
             patch.object(sessions.os, "execvpe") as execute:
            sessions.list_and_attach("Test")
        self.assertEqual(execute.call_args.args[1], ["zmx", "attach", B, "/bin/false"])

        with patch.object(sessions, "coordinator_snapshot", side_effect=[state, {**state, "revision": 2}]), \
             patch.object(sessions, "choose", return_value=B), \
             patch.object(sessions, "live_sessions", return_value={A, B}), \
             patch.object(sessions.os, "execvpe") as execute:
            with self.assertRaisesRegex(ValueError, "changed"):
                sessions.list_and_attach("Test")
            execute.assert_not_called()

    def test_cancel_does_not_attach(self):
        state = {"exists": True, "revision": 1, "snapshot": LAYOUT, "panes": [A, B, C]}
        with patch.object(sessions, "coordinator_snapshot", return_value=state), \
             patch.object(sessions, "choose", return_value=None), \
             patch.object(sessions, "live_sessions", return_value={A}), \
             patch.object(sessions.os, "execvpe") as execute:
            self.assertEqual(sessions.list_and_attach("Test"), 0)
            execute.assert_not_called()

    def test_disappeared_session_or_listing_failure_never_attaches(self):
        state = {"exists": True, "revision": 1, "snapshot": LAYOUT, "panes": [A, B, C]}
        for second in (set(), OSError("zmx unavailable")):
            with self.subTest(second=second), \
                 patch.object(sessions, "coordinator_snapshot", return_value=state), \
                 patch.object(sessions, "choose", return_value=B), \
                 patch.object(sessions, "live_sessions", side_effect=[{B}, second]), \
                 patch.object(sessions.os, "execvpe") as execute:
                with self.assertRaises((ValueError, OSError)):
                    sessions.list_and_attach("Test")
                execute.assert_not_called()
