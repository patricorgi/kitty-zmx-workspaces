import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kzw.cli import capture_lock, create_pane, close_target, detach

A = "kzw-" + "a" * 32
B = "kzw-" + "b" * 32


def tree(panes):
    return [{"tabs": [{"windows": [{"id": i, "user_vars": {"kzw_pane": pane, "kzw_host": ""}}
                                    for i, pane in panes]}]}]


class ManagedCreateTests(unittest.TestCase):
    def test_local_bootstrap_recovery_requires_original_session_after_launch(self):
        from kzw.cli import EMPTY_SNAPSHOT, mark_bootstrap_launching, prepare_bootstrap, recover_bootstrap
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "workspace.kitty-session").write_text(EMPTY_SNAPSHOT)
            (path / "panes.json").write_text("[]\n")
            ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
            with patch("kzw.cli.uuid.uuid4", side_effect=ids), patch("kzw.cli.zmx_command", return_value=""):
                session = prepare_bootstrap(path, "Test", "host", "/bin/kzw-pane", allow_empty=True)
            mark_bootstrap_launching(path)
            with patch("kzw.cli.zmx_command", return_value=""):
                with self.assertRaisesRegex(ValueError, "refusing to recreate"):
                    recover_bootstrap(path, "Test", "host", "/bin/kzw-pane")
            with patch("kzw.cli.zmx_command", return_value=A + "\n"):
                recover_bootstrap(path, "Test", "host", "/bin/kzw-pane")
            self.assertIn("--existing", session.read_text())

    def test_local_bootstrap_recovers_after_snapshot_saved_before_journal_removal(self):
        from kzw.cli import mark_bootstrap_launching, prepare_bootstrap, reconcile_local_bootstrap_commit
        from kzw.core import rewrite_export
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
            with patch("kzw.cli.uuid.uuid4", side_effect=ids), patch("kzw.cli.zmx_command", return_value=""):
                session = prepare_bootstrap(path, "Test", "host", "/bin/kzw-pane")
            mark_bootstrap_launching(path)
            export = "new_tab Test\nlaunch 'kitty-unserialize-data={\"id\":1}'\n"
            (path / "workspace.kitty-session").write_text(rewrite_export(export, {1: (A, "host")}, "/bin/kzw-pane"))
            (path / "panes.json").write_text(json.dumps([A]))
            self.assertTrue(reconcile_local_bootstrap_commit(path, "Test", "host", "/bin/kzw-pane"))
            self.assertFalse((path / "pending-create.json").exists())
            self.assertFalse(session.exists())

    def test_local_empty_workspace_bootstrap_preserves_empty_until_capture(self):
        from kzw.cli import EMPTY_SNAPSHOT, finish_bootstrap, mark_bootstrap_launching, prepare_bootstrap
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "workspace.kitty-session").write_text(EMPTY_SNAPSHOT)
            (path / "panes.json").write_text("[]\n")
            ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
            with patch("kzw.cli.uuid.uuid4", side_effect=ids), patch("kzw.cli.zmx_command", return_value=""):
                session = prepare_bootstrap(path, "Test", "host", "/bin/kzw-pane", allow_empty=True)
            self.assertEqual((path / "workspace.kitty-session").read_text(), EMPTY_SNAPSHOT)
            mark_bootstrap_launching(path)
            current = [{"last_focused": True, "tabs": [{"is_active": True, "windows": [
                {"id": 1, "is_active": True, "user_vars": {"kzw_pane": A, "kzw_host": "host"}}]}]}]
            with patch("kzw.cli.query", return_value=json.dumps(current)), \
                 patch("kzw.cli.zmx_command", return_value=A + "\n"), \
                 patch("kzw.cli.capture_locked") as capture:
                finish_bootstrap(path, "/bin/kzw-pane")
            self.assertEqual(capture.call_args.kwargs["expected_panes"], {1: (A, "host")})
            self.assertFalse(session.exists())
            self.assertFalse((path / "pending-create.json").exists())

    def test_remote_bootstrap_prepares_before_kitty_and_finishes_before_snapshot_loop(self):
        from kzw.cli import run
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "remote.json").write_text("{}")
            bootstrap = path / "bootstrap.kitty-session"
            child = Mock(returncode=0)
            child.poll.side_effect = [None, 0]
            title_stop = Mock()
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.prepare_bootstrap", return_value=bootstrap) as prepare, \
                 patch("kzw.cli.mark_bootstrap_launching") as mark, \
                 patch("kzw.cli.finish_bootstrap") as finish, \
                 patch("kzw.cli.start_title_monitor", return_value=title_stop), \
                 patch("kzw.cli.remote_client.start_status_monitor", return_value=lambda: None), \
                 patch("kzw.cli.subprocess.Popen", return_value=child) as popen, \
                 patch("kzw.cli.synchronize_exits"), patch("kzw.cli.capture"):
                self.assertEqual(run("Test", "origin", remote_attached=True), 0)
            prepare.assert_called_once_with(path, "Test", "origin", "/bin/kzw-pane")
            mark.assert_called_once_with(path)
            finish.assert_called_once_with(path, "/bin/kzw-pane")
            self.assertIn(str(bootstrap), popen.call_args.args[0])
            title_stop.set.assert_called_once()

    def test_create_saves_only_after_verified_identity_and_session(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("kzw.cli.state_dir", return_value=Path(tmp)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.uuid.uuid4") as uid, \
             patch("kzw.cli.capture_locked") as capture, \
             patch("kzw.cli.query", side_effect=[json.dumps(tree([(1, A)])), "2", json.dumps(tree([(1, A), (2, B)]))]) as query, \
             patch("kzw.cli.zmx_command", side_effect=["", B + "\n"]):
            uid.return_value.hex = "b" * 32
            create_pane("Test", "right", 1, A, "")
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(capture.call_args.kwargs["expected_panes"], {1: (A, ""), 2: (B, "")})
            self.assertIn("--location=hsplit", query.call_args_list[1].args)
            self.assertFalse((Path(tmp) / "pending-create.json").exists())

    def test_failed_launch_keeps_journal_and_blocks_close_and_detach(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("kzw.cli.state_dir", return_value=Path(tmp)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked"), \
             patch("kzw.cli.zmx_command", return_value=""), \
             patch("kzw.cli.query", side_effect=[json.dumps(tree([(1, A)])), OSError("lost reply")]):
            with self.assertRaises(OSError):
                create_pane("Test", "tab", 1, A, "")
            self.assertTrue((Path(tmp) / "pending-create.json").exists())
            with self.assertRaisesRegex(ValueError, "creation is incomplete"):
                close_target("Test", "pane", 1)
            with self.assertRaisesRegex(ValueError, "creation is incomplete"):
                detach("Test")

    def test_changed_target_does_not_launch(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("kzw.cli.state_dir", return_value=Path(tmp)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked"), \
             patch("kzw.cli.query", return_value=json.dumps(tree([(1, B)]))) as query:
            with self.assertRaisesRegex(ValueError, "target changed"):
                create_pane("Test", "below", 1, A, "")
            self.assertEqual(query.call_count, 1)
            self.assertFalse((Path(tmp) / "pending-create.json").exists())

    def test_creation_waits_for_workspace_mutation_lock(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("kzw.cli.state_dir", return_value=Path(tmp)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked") as capture, \
             patch("kzw.cli.query", return_value=json.dumps(tree([(1, B)]))):
            started = threading.Event()
            finished = threading.Event()
            def worker():
                started.set()
                try:
                    create_pane("Test", "right", 1, A, "")
                except ValueError:
                    pass
                finally:
                    finished.set()
            with capture_lock(Path(tmp)):
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(0.1))
                capture.assert_not_called()
            thread.join(timeout=2)
            self.assertTrue(finished.is_set())
