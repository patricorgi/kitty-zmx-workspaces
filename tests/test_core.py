import json
import shlex
import tempfile
import unittest
import io
import types
from pathlib import Path
from unittest.mock import patch

from kzw.core import atomic_write, check_name, focused_pane, pane_map, rewrite_export, saved_focus
from kzw.cli import EMPTY_SNAPSHOT, capture, capture_locked, cleanup_close_locked, close_pane, close_tab, detach, restore_focus, synchronize_exits
from kzw import pane


A = "kzw-" + "a" * 32
B = "kzw-" + "b" * 32


def tree():
    return [{"last_focused": True, "tabs": [{"is_active": True, "windows": [
        {"id": 7, "is_active": True, "user_vars": {"kzw_pane": A, "kzw_host": ""}},
        {"id": 8, "is_active": False, "user_vars": {"kzw_pane": B, "kzw_host": "dev.example"}},
    ]}]}]


def launch(window_id, extra=""):
    metadata = json.dumps({"id": window_id, "cmd_at_shell_startup": "rm -rf never"})
    return shlex.join(["launch", "kitty-unserialize-data=" + metadata, "--cwd=/some path", "--var=example=yes", *shlex.split(extra)])


class WorkspaceTests(unittest.TestCase):
    def test_lifecycle_event_format_uses_validated_stable_pane_ids(self):
        from kzw.cli import log_pane_event
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            log_pane_event("tab_created", [A])
            log_pane_event("tab_destroyed", [B, A])
        self.assertEqual(output.getvalue(),
                         "tab_created:" + "a" * 32 + "\n" +
                         "tab_destroyed:" + "a" * 32 + "," + "b" * 32 + "\n")
        with self.assertRaises(ValueError):
            log_pane_event("tab_destroyed", ["not-a-pane"])

    def test_local_empty_retirement_preserves_unfinished_operations(self):
        from kzw.cli import retire_local_empty
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            snapshot = path / "workspace.kitty-session"
            metadata = path / "panes.json"
            snapshot.write_text(EMPTY_SNAPSHOT)
            metadata.write_text("[]\n")
            journal = path / "pending-close.json"
            journal.write_text("{}\n")
            self.assertFalse(retire_local_empty(path))
            self.assertTrue(snapshot.exists())
            journal.unlink()
            self.assertTrue(retire_local_empty(path))
            self.assertFalse(snapshot.exists())
            self.assertFalse(metadata.exists())

    def test_background_kitty_forwards_local_test_session_environment(self):
        from kzw.cli import kitty_launch_command
        env = {"KZW_TEST_KITTY_BACKGROUND": "1", "ZMX_DIR": "/private/test-zmx",
               "SHELL": "/bin/bash", "PATH": "/bin", "UNRELATED": "private"}
        with patch("kzw.cli.sys.platform", "darwin"), patch("kzw.cli.Path.is_dir", return_value=True):
            command = kitty_launch_command(["kitty", "--session", "none"], env)
        self.assertIn("ZMX_DIR=/private/test-zmx", command)
        self.assertIn("SHELL=/bin/bash", command)
        self.assertNotIn("UNRELATED=private", command)
        self.assertEqual(command[-3:], ["--args", "--session", "none"])

    def test_rewrites_commands_without_losing_layout(self):
        source = "\n".join(["new_tab work", "layout splits", launch(7, "echo bad"), launch(8),
                            'set_layout_state {"pairs":{"one":7,"two":8}}'])
        out = rewrite_export(source, pane_map(tree()), "/a path/kzw-pane")
        self.assertIn('set_layout_state {"pairs":{"one":7,"two":8}}', out)
        self.assertNotIn("rm -rf", out)
        self.assertNotIn("echo bad", out)
        launches = [shlex.split(line) for line in out.splitlines() if line.startswith("launch ")]
        self.assertEqual(launches[0][-6:], ["/a path/kzw-pane", "--pane", A, "--host", "", "--existing"])
        self.assertEqual(launches[1][-6:], ["/a path/kzw-pane", "--pane", B, "--host", "dev.example", "--existing"])

    def test_incomplete_and_duplicate_panes_fail_closed(self):
        with self.assertRaises(ValueError):
            rewrite_export(launch(7), pane_map(tree()), "/bin/kzw-pane")
        bad = tree()
        bad[0]["tabs"][0]["windows"][1]["user_vars"]["kzw_pane"] = A
        with self.assertRaises(ValueError):
            pane_map(bad)

    def test_unmanaged_pane_fails_closed(self):
        bad = tree()
        bad[0]["tabs"][0]["windows"][0]["user_vars"] = {}
        with self.assertRaises(ValueError):
            pane_map(bad)

    def test_invalid_export_fails_closed(self):
        with self.assertRaises(ValueError):
            rewrite_export("launch --cwd=/tmp bash", pane_map(tree()), "/bin/kzw-pane")

    def test_transient_attachment_ended_marker_not_restored(self):
        output = rewrite_export(launch(7) + " --var=kzw_attach_ended=1\n" + launch(8),
                                pane_map(tree()), "/bin/kzw-pane")
        self.assertNotIn("kzw_attach_ended", output)

    def test_workspace_path_validation(self):
        self.assertEqual(check_name("work-1"), "work-1")
        for name in ("../oops", "a/b", "", ".hidden"):
            with self.assertRaises(ValueError):
                check_name(name)

    def test_atomic_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "work" / "snapshot"
            atomic_write(path, "first")
            atomic_write(path, "second")
            self.assertEqual(path.read_text(), "second")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_capture_refuses_removed_panes_until_explicitly_allowed(self):
        current = tree()
        current[0]["tabs"][0]["windows"].pop()
        export = launch(7) + "\n"
        def query(_socket, *args):
            return export if "--output-format=session" in args else json.dumps(current)
        with tempfile.TemporaryDirectory() as directory, patch("kzw.cli.query", side_effect=query):
            path = Path(directory)
            (path / "panes.json").write_text(json.dumps([A, B]))
            (path / "workspace.kitty-session").write_text("previous complete snapshot")
            with self.assertRaises(ValueError):
                capture(path, "/bin/kzw-pane")
            self.assertEqual((path / "workspace.kitty-session").read_text(), "previous complete snapshot")
            self.assertEqual(capture(path, "/bin/kzw-pane", allow_removals=True), 1)

    def test_capture_refuses_racing_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            responses = [json.dumps(tree()), launch(7) + "\n" + launch(8),
                         json.dumps(tree()), launch(7) + "\n" + launch(8) + "\nfocus"]
            with patch("kzw.cli.query", side_effect=responses):
                with self.assertRaises(ValueError):
                    capture(Path(directory), "/bin/kzw-pane")
            self.assertFalse((Path(directory) / "workspace.kitty-session").exists())

    def test_capture_persists_focus_inside_snapshot(self):
        current = tree()
        current[0]["tabs"][0]["windows"][0]["is_active"] = False
        current[0]["tabs"][0]["windows"][1]["is_active"] = True
        export = launch(7) + "\n" + launch(8)
        def query(_socket, *args):
            return export if "--output-format=session" in args else json.dumps(current)
        with tempfile.TemporaryDirectory() as directory, patch("kzw.cli.query", side_effect=query), \
             patch("kzw.cli.zmx_command", return_value=A + "\n" + B + "\n"):
            capture(Path(directory), "/bin/kzw-pane")
            text = (Path(directory) / "workspace.kitty-session").read_text()
            self.assertEqual(saved_focus(text), B)

    def test_local_capture_preserves_native_journal_until_session_is_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Test"
            path.mkdir()
            (path / "panes.json").write_text(json.dumps([A]))
            (path / "workspace.kitty-session").write_text("previous complete snapshot\n")
            root = path / "native-pending"
            root.mkdir(mode=0o700)
            journal = root / (B + ".json")
            journal.write_text(json.dumps({"pane": B, "host": "dev.example", "workspace": "Test"}))
            export = launch(7) + "\n" + launch(8)
            def kitty(_socket, *args):
                return export if "--output-format=session" in args else json.dumps(tree())
            with patch("kzw.cli.query", side_effect=kitty), patch("kzw.cli.zmx_command", return_value=""):
                with self.assertRaisesRegex(ValueError, "not yet ready"):
                    capture(path, "/bin/kzw-pane")
            self.assertTrue(journal.exists())
            self.assertEqual((path / "workspace.kitty-session").read_text(), "previous complete snapshot\n")
            with patch("kzw.cli.query", side_effect=kitty), patch("kzw.cli.zmx_command", return_value=B + "\n"):
                capture(path, "/bin/kzw-pane")
            self.assertFalse(journal.exists())
            self.assertEqual(set(json.loads((path / "panes.json").read_text())), {A, B})

    def test_unregistered_native_journal_blocks_restore(self):
        from kzw.cli import check_native_journals
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Test"
            root = path / "native-pending"
            root.mkdir(parents=True, mode=0o700)
            journal = root / (B + ".json")
            journal.write_text(json.dumps({"pane": B, "host": "dev.example", "workspace": "Test"}))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                check_native_journals(path)
            self.assertTrue(journal.exists())

    def test_native_stage_recovery_requires_all_sessions_and_preserves_baseline(self):
        from kzw.cli import recover_local_native_stage
        from kzw.core import rewrite_export
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Test"
            path.mkdir()
            baseline = "previous complete snapshot\n"
            target = path / "workspace.kitty-session"
            target.write_text(baseline)
            (path / "panes.json").write_text(json.dumps([A]))
            pending = path / "native-pending"
            pending.mkdir(mode=0o700)
            journal = pending / (B + ".json")
            journal.write_text(json.dumps({"pane": B, "host": "dev.example", "workspace": "Test"}))
            snapshot = rewrite_export(launch(7) + "\n" + launch(8),
                                      {7: (A, "dev.example"), 8: (B, "dev.example")}, "/bin/kzw-pane")
            stage = path / "native-stage.json"
            stage.write_text(json.dumps({"kind": "kzw-native-stage-v1", "snapshot": snapshot,
                                         "base_hash": hashlib.sha256(baseline.encode()).hexdigest(),
                                         "panes": [A, B], "added": [B]}))
            with patch("kzw.cli.zmx_command", return_value=A + "\n"):
                with self.assertRaisesRegex(ValueError, "refusing to recreate"):
                    recover_local_native_stage(path, "/bin/kzw-pane")
            self.assertEqual(target.read_text(), baseline)
            self.assertTrue(stage.exists())
            self.assertTrue(journal.exists())
            with patch("kzw.cli.zmx_command", return_value=A + "\n" + B + "\n"):
                self.assertTrue(recover_local_native_stage(path, "/bin/kzw-pane"))
            self.assertEqual(target.read_text(), snapshot)
            self.assertFalse(stage.exists())
            self.assertFalse(journal.exists())

    def test_managed_stage_recovery_retries_after_commit_acknowledgement_loss(self):
        from kzw.cli import recover_local_managed_creation
        from kzw.core import rewrite_export
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Test"
            path.mkdir()
            baseline = "previous complete snapshot\n"
            (path / "workspace.kitty-session").write_text(baseline)
            (path / "panes.json").write_text(json.dumps([A]))
            root = path / "native-pending"
            root.mkdir(mode=0o700)
            (root / (B + ".json")).write_text(json.dumps({"pane": B, "host": "dev.example", "workspace": "Test"}))
            snapshot = rewrite_export(launch(7) + "\n" + launch(8),
                                      {7: (A, "dev.example"), 8: (B, "dev.example")}, "/bin/kzw-pane")
            (path / "native-stage.json").write_text(json.dumps({"kind": "kzw-native-stage-v1", "snapshot": snapshot,
                "base_hash": hashlib.sha256(baseline.encode()).hexdigest(), "panes": [A, B], "added": [B]}))
            pending = path / "pending-create.json"
            pending.write_text(json.dumps({"kind": "right", "pane": B, "host": "dev.example", "target": A,
                                           "request_id": "a" * 32}))
            with patch("kzw.cli.zmx_command", return_value=A + "\n"):
                with self.assertRaisesRegex(ValueError, "refusing to recreate"):
                    recover_local_managed_creation(path, "/bin/kzw-pane")
            self.assertTrue(pending.exists())
            self.assertEqual((path / "workspace.kitty-session").read_text(), baseline)
            unlink = Path.unlink
            def interrupted(file, *args, **kwargs):
                if file == pending:
                    raise OSError("interrupted")
                return unlink(file, *args, **kwargs)
            with patch("kzw.cli.zmx_command", return_value=A + "\n" + B + "\n"):
                with patch("pathlib.Path.unlink", autospec=True, side_effect=interrupted):
                    with self.assertRaises(OSError):
                        recover_local_managed_creation(path, "/bin/kzw-pane")
                self.assertFalse((path / "native-stage.json").exists())
                self.assertTrue(recover_local_managed_creation(path, "/bin/kzw-pane"))
            self.assertFalse(pending.exists())

    def test_remote_managed_stage_recovery_retains_journal_until_status_confirmed(self):
        from kzw.cli import recover_remote_managed_creation
        from kzw.core import rewrite_export
        from kzw import remote_client
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Test"
            path.mkdir()
            pending = path / "pending-create.json"
            pending.write_text(json.dumps({"kind": "tab", "pane": B, "host": "origin", "target": A,
                                           "request_id": "a" * 32}))
            snapshot = rewrite_export(launch(7) + "\n" + launch(8),
                                      {7: (A, "origin"), 8: (B, "origin")}, "/bin/kzw-pane")
            portable, panes = remote_client.translate(snapshot, "", "origin", True)
            remote = {"revision": 1, "panes": [A], "reservations": [B],
                      "staged": {"revision": 1, "snapshot": portable, "panes": panes}}
            committed = {"revision": 2, "panes": panes, "reservations": [], "staged": None,
                         "snapshot": portable}
            with patch("kzw.cli.remote_client.call", side_effect=[remote, committed]), \
                 patch("kzw.cli.remote_client.pull"), \
                 patch("kzw.cli.remote_client.status", side_effect=[OSError("offline"), {"live": panes, "missing": []}]):
                with self.assertRaises(OSError):
                    recover_remote_managed_creation(path, "Test", "origin", "/bin/kzw-pane")
                self.assertTrue(pending.exists())
                self.assertTrue(recover_remote_managed_creation(path, "Test", "origin", "/bin/kzw-pane"))
            self.assertFalse(pending.exists())

    def test_focus_change_during_capture_fails_closed(self):
        before, after = tree(), tree()
        after[0]["tabs"][0]["windows"][0]["is_active"] = False
        after[0]["tabs"][0]["windows"][1]["is_active"] = True
        with tempfile.TemporaryDirectory() as directory:
            with patch("kzw.cli.query", side_effect=[json.dumps(before), "export", json.dumps(after)]):
                with self.assertRaises(ValueError):
                    capture(Path(directory), "/bin/kzw-pane")
            self.assertFalse((Path(directory) / "workspace.kitty-session").exists())

    def test_restore_focus_uses_stable_pane_id(self):
        after = tree()
        after[0]["tabs"][0]["windows"][0]["is_active"] = False
        after[0]["tabs"][0]["windows"][1]["is_active"] = True
        with patch("kzw.cli.query", side_effect=[json.dumps(tree()), "", json.dumps(after)]) as query:
            restore_focus(Path("socket"), B)
        self.assertEqual(query.call_args_list[1].args[1:], ("focus-window", "--match", "id:8"))
        self.assertEqual(focused_pane(after), B)

    def test_failed_detach_capture_does_not_close_windows(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.state_dir", return_value=Path(directory)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked", side_effect=ValueError("incomplete")), \
             patch("kzw.cli.query") as query:
            with self.assertRaises(ValueError):
                detach("Test")
            query.assert_not_called()

    def test_detach_retries_transient_layout_change_before_closing(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.state_dir", return_value=Path(directory)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked", side_effect=[ValueError("layout changed while taking snapshot"), 2]) as capture, \
             patch("kzw.cli.time.sleep"), patch("kzw.cli.query") as query:
            detach("Test")
            self.assertEqual(capture.call_count, 2)
            query.assert_called_once()

    def test_cleanup_refuses_incomplete_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending-close.json"
            path.write_text(json.dumps({"phase": "closing", "pane": A, "host": ""}))
            with patch("kzw.cli.zmx_command") as zmx:
                with self.assertRaises(ValueError):
                    cleanup_close_locked(Path(directory))
                zmx.assert_not_called()
            self.assertTrue(path.exists())

    def test_close_capture_rejects_additional_pane_changes(self):
        current = tree()
        export = launch(7) + "\n" + launch(8)
        def query(_socket, *args):
            return export if "--output-format=session" in args else json.dumps(current)
        with tempfile.TemporaryDirectory() as directory, patch("kzw.cli.query", side_effect=query):
            with self.assertRaises(ValueError):
                capture_locked(Path(directory), "/bin/kzw-pane", allow_removals=True, expected_panes={7: (A, "")})
            self.assertFalse((Path(directory) / "workspace.kitty-session").exists())

    def test_cleanup_preserves_record_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending-close.json"
            path.write_text(json.dumps({"phase": "layout-saved", "pane": A, "host": ""}))
            with patch("kzw.cli.zmx_command", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    cleanup_close_locked(Path(directory))
            self.assertTrue(path.exists())

    def test_tab_cleanup_journals_partial_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending-close.json"
            path.write_text(json.dumps({"phase": "layout-saved", "panes": [
                {"pane": A, "host": ""}, {"pane": B, "host": ""}]}))
            with patch("kzw.cli.zmx_command", side_effect=[A + "\n", "", "", OSError("offline")]) as zmx:
                with self.assertRaises(OSError):
                    cleanup_close_locked(Path(directory))
            self.assertEqual(json.loads(path.read_text())["panes"], [{"pane": B, "host": ""}])
            self.assertEqual(zmx.call_args_list[1].args, ("", "kill", A))
            with patch("kzw.cli.zmx_command", return_value=""):
                self.assertEqual(cleanup_close_locked(Path(directory)), [B])
            self.assertFalse(path.exists())

    def test_invalid_tab_rejected_without_terminating(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.state_dir", return_value=Path(directory)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked"), \
             patch("kzw.cli.query", return_value=json.dumps(tree())), \
             patch("kzw.cli.zmx_command") as zmx:
            with self.assertRaises(ValueError):
                close_tab("Test", 999)
            zmx.assert_not_called()

    def test_close_refuses_unknown_pane_before_destroying_anything(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.state_dir", return_value=Path(directory)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked"), \
             patch("kzw.cli.query", return_value=json.dumps(tree())) as query, \
             patch("kzw.cli.zmx_command") as zmx:
            with self.assertRaises(ValueError):
                close_pane("Test", 999)
            zmx.assert_not_called()
            self.assertEqual(query.call_count, 1)

    def test_confirmed_selection_change_is_rejected(self):
        from kzw.cli import close_target
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.state_dir", return_value=Path(directory)), \
             patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
             patch("kzw.cli.capture_locked"), \
             patch("kzw.cli.query", return_value=json.dumps(tree())) as query, \
             patch("kzw.cli.zmx_command") as zmx:
            with self.assertRaisesRegex(ValueError, "changed after confirmation"):
                close_target("Test", "pane", 7, confirmed_selection={"7": [B, ""]})
            self.assertEqual(query.call_count, 1)
            zmx.assert_not_called()
            self.assertFalse((Path(directory) / "pending-close.json").exists())

    def test_cli_close_cancellation_never_mutates(self):
        from kzw.cli import main
        for command, option in (("close-pane", "--window-id"), ("close-tab", "--tab-id")):
            with self.subTest(command=command), \
                 patch("sys.argv", ["kzw", command, "Test", option, "7"]), \
                 patch("builtins.input", return_value=""), \
                 patch("kzw.cli.close_pane") as pane_close, \
                 patch("kzw.cli.close_tab") as tab_close:
                self.assertEqual(main(), 0)
                pane_close.assert_not_called()
                tab_close.assert_not_called()

    def test_server_stop_requires_confirmation(self):
        from kzw.cli import main
        with patch("sys.argv", ["kzw", "server-stop", "--host", "origin"]), \
             patch("builtins.input", return_value=""), patch("kzw.cli.remote_client.call") as call:
            self.assertEqual(main(), 0)
            call.assert_not_called()
        with patch("sys.argv", ["kzw", "server-stop", "--host", "origin"]), \
             patch("builtins.input", return_value="y"), patch("kzw.cli.remote_client.call") as call:
            self.assertEqual(main(), 0)
            call.assert_called_once_with("origin", {"method": "server.shutdown"})

    def test_new_pane_is_an_explicit_empty_workspace_operation(self):
        from kzw.cli import main
        with patch("sys.argv", ["kzw", "new-pane", "Test", "--host", "origin"]), \
             patch("kzw.cli.attach_remote", return_value=0) as attach:
            self.assertEqual(main(), 0)
        attach.assert_called_once_with("Test", "origin", create_empty=True)
        with patch("sys.argv", ["kzw", "new-pane", "Test", "--shell-host", "origin"]), \
             patch("kzw.cli.run", return_value=0) as run:
            self.assertEqual(main(), 0)
        run.assert_called_once_with("Test", "origin", create_empty=True)

    def test_remote_close_refuses_lost_lease_before_changing_kitty(self):
        from kzw.cli import close_target
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "remote.json").write_text("{}")
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.remote_client.renew_lease", side_effect=ValueError("lease lost")), \
                 patch("kzw.cli.query") as query:
                with self.assertRaisesRegex(ValueError, "lease lost"):
                    close_target("Test", "pane", 7, "origin")
                query.assert_not_called()

    def test_managed_close_refuses_persistent_unmanaged_overlay(self):
        from kzw.cli import close_target
        message = "a Kitty pane is not managed by kzw; refusing to save an incomplete workspace"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.capture_locked", side_effect=ValueError(message)) as capture, \
                 patch("kzw.cli.time.sleep"), patch("kzw.cli.query") as query:
                with self.assertRaisesRegex(ValueError, "not managed"):
                    close_target("Test", "pane", 7)
            self.assertEqual(capture.call_count, 30)
            query.assert_not_called()
            self.assertFalse((path / "pending-close.json").exists())

    def test_capture_retries_transient_confirmation_overlay(self):
        from kzw.cli import capture_stable_locked
        message = "a Kitty pane is not managed by kzw; refusing to save an incomplete workspace"
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.capture_locked", side_effect=[ValueError(message), 1]) as capture, \
             patch("kzw.cli.time.sleep"):
            self.assertEqual(capture_stable_locked(Path(directory), "/bin/kzw-pane"), 1)
        self.assertEqual(capture.call_count, 2)

    def test_retry_create_finishes_only_matching_live_pane(self):
        from kzw.cli import retry_create
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "panes.json").write_text(json.dumps([A]))
            pending = path / "pending-create.json"
            pending.write_text(json.dumps({"pane": B, "host": "dev.example"}))
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.query", return_value=json.dumps(tree())), \
                 patch("kzw.cli.zmx_command", return_value=B + "\n"), \
                 patch("kzw.cli.capture_locked") as capture:
                retry_create("Test")
            self.assertFalse(pending.exists())
            self.assertEqual(capture.call_args.kwargs["expected_panes"], pane_map(tree()))

    def test_retry_create_preserves_journal_when_session_missing(self):
        from kzw.cli import retry_create
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "panes.json").write_text(json.dumps([A]))
            pending = path / "pending-create.json"
            pending.write_text(json.dumps({"pane": B, "host": "dev.example"}))
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.query", return_value=json.dumps(tree())), \
                 patch("kzw.cli.zmx_command", return_value=""), \
                 patch("kzw.cli.capture_locked") as capture:
                with self.assertRaisesRegex(ValueError, "missing"):
                    retry_create("Test")
            self.assertTrue(pending.exists())
            capture.assert_not_called()

    def test_retry_partial_managed_close_saves_exact_survivors_before_cleanup(self):
        from kzw.cli import retry_close
        before = pane_map(tree())
        survivor = tree()
        survivor[0]["tabs"][0]["windows"] = survivor[0]["tabs"][0]["windows"][:1]
        record = {"phase": "closing", "remote": False, "last": False, "kind": "pane", "target_id": 8,
                  "panes": [{"pane": B, "host": "dev.example"}],
                  "before": {str(key): list(value) for key, value in before.items()}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "pending-close.json").write_text(json.dumps(record))
            def cleanup(directory):
                self.assertEqual(json.loads((directory / "pending-close.json").read_text())["phase"], "layout-saved")
                return [B]
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.query", return_value=json.dumps(survivor)) as query, \
                 patch("kzw.cli.capture_locked") as capture, \
                 patch("kzw.cli.cleanup_close_locked", side_effect=cleanup):
                retry_close("Test")
            self.assertEqual(query.call_count, 1)
            self.assertEqual(capture.call_args.kwargs["expected_panes"], {7: before[7]})
            self.assertTrue(capture.call_args.kwargs["allow_removals"])

    def test_retry_partial_close_refuses_unrelated_layout_change(self):
        from kzw.cli import retry_close
        before = pane_map(tree())
        changed = tree()
        changed[0]["tabs"][0]["windows"][0]["user_vars"]["kzw_pane"] = "kzw-" + "c" * 32
        record = {"phase": "closing", "remote": False, "last": False, "kind": "pane", "target_id": 8,
                  "panes": [{"pane": B, "host": "dev.example"}],
                  "before": {str(key): list(value) for key, value in before.items()}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            pending = path / "pending-close.json"
            pending.write_text(json.dumps(record))
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.query", return_value=json.dumps(changed)) as query, \
                 patch("kzw.cli.cleanup_close_locked") as cleanup:
                with self.assertRaisesRegex(ValueError, "differs"):
                    retry_close("Test")
            self.assertEqual(query.call_count, 1)
            cleanup.assert_not_called()
            self.assertEqual(json.loads(pending.read_text()), record)

    def test_retry_partial_close_can_resume_exact_confirmed_view(self):
        from kzw.cli import retry_close
        before = pane_map(tree())
        survivor = tree()
        survivor[0]["tabs"][0]["windows"] = survivor[0]["tabs"][0]["windows"][:1]
        record = {"phase": "closing", "remote": False, "last": False, "kind": "pane", "target_id": 8,
                  "panes": [{"pane": B, "host": "dev.example"}],
                  "before": {str(key): list(value) for key, value in before.items()}}
        replies = iter([json.dumps(tree()), "", json.dumps(survivor)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "pending-close.json").write_text(json.dumps(record))
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.query", side_effect=lambda *args: next(replies)) as query, \
                 patch("kzw.cli.capture_locked"), \
                 patch("kzw.cli.cleanup_close_locked", return_value=[B]):
                retry_close("Test")
            query.assert_any_call(path / "kitty.sock", "close-window", "--match", "id:8")

    def test_retry_last_close_commits_empty_before_cleanup(self):
        from kzw.cli import retry_close
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.state_dir", return_value=Path(directory)), \
             patch("kzw.cli.zmx_command", return_value=""):
            path = Path(directory)
            (path / "pending-close.json").write_text(json.dumps({
                "phase": "closing", "last": True, "remote": False,
                "panes": [{"pane": A, "host": ""}]}))
            retry_close("Test")
            self.assertFalse((path / "workspace.kitty-session").exists())
            self.assertFalse((path / "panes.json").exists())
            self.assertFalse((path / "pending-close.json").exists())

    def test_missing_local_session_does_not_attach(self):
        result = __import__("subprocess").CompletedProcess(["zmx"], 0, stdout="another-session\n")
        with patch("sys.argv", ["kzw-pane", "--pane", A, "--host", "", "--existing"]), \
             patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO), \
             patch("kzw.pane.report_attachment_ended"), patch("kzw.pane.subprocess.run", return_value=result) as run:
            pane.main()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["zmx", "list", "--short"])

    def test_attachment_status_is_only_printed_in_debug_mode(self):
        from subprocess import CompletedProcess
        for debug, expected in ((None, False), ("0", False), ("1", True)):
            with self.subTest(debug=debug), patch.dict("os.environ", {}, clear=True), \
                 patch("sys.argv", ["kzw-pane", "--pane", A]), \
                 patch("sys.stdout", new_callable=io.StringIO), \
                 patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                 patch("kzw.pane.report_attachment_ended") as ended, \
                 patch("kzw.pane.subprocess.run", return_value=CompletedProcess([], 0)):
                if debug is not None:
                    __import__("os").environ["KZW_DEBUG"] = debug
                pane.main()
                self.assertEqual("attachment ended; checking whether" in stderr.getvalue(), expected)
                ended.assert_called_once_with(retry=False)

    def test_attachment_errors_remain_visible_without_debug(self):
        from subprocess import CompletedProcess
        with patch.dict("os.environ", {}, clear=True), patch("sys.argv", ["kzw-pane", "--pane", A]), \
             patch("sys.stdout", new_callable=io.StringIO), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr, \
             patch("kzw.pane.report_attachment_ended"), \
             patch("kzw.pane.subprocess.run", return_value=CompletedProcess([], 1)):
            pane.main()
        self.assertIn("attachment exited with status 1", stderr.getvalue())
        self.assertNotIn("checking whether the session still exists", stderr.getvalue())

    def test_remote_restore_checks_existence_before_attach(self):
        result = __import__("subprocess").CompletedProcess(["ssh"], 0)
        with patch("sys.argv", ["kzw-pane", "--pane", A, "--host", "example-host", "--existing"]), \
             patch("sys.stdout", new_callable=io.StringIO), \
             patch("kzw.pane.report_attachment_ended"), \
             patch("kzw.pane.subprocess.run", return_value=result) as run:
            pane.main()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["ssh", "-t", "example-host"])
        self.assertIn("zmx list --short", cmd[3])
        self.assertIn("exec zmx attach " + A, cmd[3])
        self.assertLess(cmd[3].index("zmx list"), cmd[3].index("exec zmx attach"))

    def test_remote_pane_does_not_attach_after_lease_loss(self):
        from kzw import remote_client
        env = {"KZW_WORKSPACE_ORIGIN": "origin", "KZW_WORKSPACE_DIRECTORY": "/private/kzw-test"}
        with patch.dict("os.environ", env), \
             patch("sys.argv", ["kzw-pane", "--pane", A, "--host", "origin", "--existing"]), \
             patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO), \
             patch.object(remote_client, "renew_lease", side_effect=ValueError("lease lost")) as renew, \
             patch("kzw.pane.subprocess.run") as attach, \
             patch("kzw.pane.report_attachment_ended", side_effect=StopIteration) as ended:
            with self.assertRaises(StopIteration):
                pane.main()
        renew.assert_called_once_with(Path("/private/kzw-test"))
        attach.assert_not_called()
        ended.assert_called_once_with(retry=True)

    def test_remote_pane_rejects_conflicting_origin(self):
        from kzw import remote_client
        with patch.dict("os.environ", {"KZW_WORKSPACE_ORIGIN": "origin", "KZW_WORKSPACE_DIRECTORY": "/private/kzw-test"}), \
             patch.object(remote_client, "renew_lease") as renew:
            with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                pane.verify_workspace_lease("other")
        renew.assert_not_called()

    def test_native_remote_pane_retries_reservation_before_first_attach(self):
        from subprocess import CompletedProcess
        ids = [types.SimpleNamespace(hex="a" * 32), types.SimpleNamespace(hex="b" * 32)]
        env = {"KZW_WORKSPACE_ORIGIN": "origin", "KZW_WORKSPACE_DIRECTORY": "/private/kzw-test"}
        with patch.dict("os.environ", env), patch("sys.argv", ["kzw-pane", "--host", "origin"]), \
             patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO), \
             patch("kzw.pane.uuid.uuid4", side_effect=ids), patch("kzw.pane.verify_workspace_lease"), \
             patch("kzw.pane.journal_new_workspace_pane"), \
             patch("kzw.pane.wait_for_native_stage"), \
             patch("kzw.pane.reserve_new_workspace_pane", side_effect=[OSError("reply lost"), None]) as reserve, \
             patch("kzw.pane.subprocess.run", return_value=CompletedProcess([], 0)) as attach, \
             patch("kzw.pane.report_attachment_ended"):
            pane.main()
        self.assertEqual(reserve.call_count, 2)
        self.assertEqual(reserve.call_args_list[0].args, (A, "b" * 32))
        self.assertEqual(reserve.call_args_list[1].args, (A, "b" * 32))
        self.assertEqual(attach.call_count, 1)
        self.assertNotIn("zmx list --short", attach.call_args.args[0][3])

    def test_native_creation_journal_is_private_idempotent_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Test"
            path.mkdir()
            with patch.dict("os.environ", {"KZW_WORKSPACE_DIRECTORY": str(path), "KZW_WORKSPACE": "Test"}):
                pane.journal_new_workspace_pane(A, "origin")
                journal = path / "native-pending" / (A + ".json")
                self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
                pane.journal_new_workspace_pane(A, "origin")
                before = journal.read_text()
                with self.assertRaisesRegex(RuntimeError, "conflicts"):
                    pane.journal_new_workspace_pane(A, "other")
                self.assertEqual(journal.read_text(), before)

    def test_native_stage_acknowledgement_must_match_journal_and_stage(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Test"
            path.mkdir()
            env = {"KZW_WORKSPACE_DIRECTORY": str(path), "KZW_WORKSPACE": "Test", "KZW_CHANGE_SIGNAL": ""}
            with patch.dict("os.environ", env):
                pane.journal_new_workspace_pane(A, "origin")
                journal = path / "native-pending" / (A + ".json")
                ready = journal.with_suffix(".ready")
                ready.write_text(hashlib.sha256(journal.read_bytes()).hexdigest() + "\n")
                stage = path / "native-stage.json"
                stage.write_text(json.dumps({"kind": "kzw-native-stage-v1", "added": [B], "panes": [A, B]}))
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    pane.wait_for_native_stage(A, "origin")
                stage.write_text(json.dumps({"kind": "kzw-native-stage-v1", "added": [A], "panes": [A]}))
                pane.wait_for_native_stage(A, "origin")

    def test_failed_remote_attachment_retries_only_existing_session(self):
        from subprocess import CompletedProcess
        results = [CompletedProcess([], 255), CompletedProcess([], 0)]
        with patch("sys.argv", ["kzw-pane", "--pane", A, "--host", "example-host"]), \
             patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO), \
             patch("kzw.pane.report_attachment_ended") as ended, \
             patch("kzw.pane.set_attachment_ended") as marker, \
             patch("kzw.pane.subprocess.run", side_effect=results) as run:
            pane.main()
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("zmx list", run.call_args_list[0].args[0][3])
        self.assertIn("zmx list --short", run.call_args_list[1].args[0][3])
        self.assertEqual([call.kwargs["retry"] for call in ended.call_args_list], [True, False])
        marker.assert_called_once_with(False)

    def test_all_confirmed_shell_exits_save_empty_workspace(self):
        current = tree()
        for window in current[0]["tabs"][0]["windows"]:
            window["user_vars"]["kzw_attach_ended"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "panes.json").write_text(json.dumps([A, B]))
            def query(_socket, *args):
                return json.dumps(current) if args == ("ls",) else ""
            with patch("kzw.cli.query", side_effect=query) as q, patch("kzw.cli.zmx_command", return_value=""), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(synchronize_exits(path, "/bin/kzw-pane"), 2)
                self.assertEqual(output.getvalue(), "tab_destroyed:" + "a" * 32 + "," + "b" * 32 + "\n")
            self.assertFalse((path / "workspace.kitty-session").exists())
            self.assertFalse((path / "panes.json").exists())
            self.assertEqual(sum(c.args[1] == "close-window" for c in q.call_args_list), 2)

    def test_network_failure_does_not_remove_panes(self):
        current = tree()
        current[0]["tabs"][0]["windows"][1]["user_vars"]["kzw_attach_ended"] = "1"
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.query", return_value=json.dumps(current)) as q, \
             patch("kzw.cli.zmx_command", side_effect=OSError("network down")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(synchronize_exits(Path(directory), "/bin/kzw-pane"), 0)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(q.call_count, 1)
            self.assertFalse((Path(directory) / "workspace.kitty-session").exists())

    def test_remote_status_failure_does_not_remove_panes(self):
        current = tree()
        current[0]["tabs"][0]["windows"][1]["user_vars"]["kzw_attach_ended"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "remote.json").write_text("{}")
            with patch("kzw.cli.query", return_value=json.dumps(current)) as query, \
                 patch("kzw.cli.remote_client.status", side_effect=OSError("origin unavailable")):
                with self.assertRaises(OSError):
                    synchronize_exits(path, "/bin/kzw-pane")
            self.assertEqual(query.call_count, 1)
            self.assertFalse((path / "workspace.kitty-session").exists())

    def test_server_hint_removes_view_only_after_confirmed_remote_absence(self):
        current = tree()
        current[0]["tabs"][0]["windows"] = [current[0]["tabs"][0]["windows"][1]]
        current[0]["tabs"][0]["windows"][0]["is_active"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "remote.json").write_text("{}")
            (path / "panes.json").write_text(json.dumps([B]))
            with patch("kzw.cli.query", return_value=json.dumps(current)) as query, \
                 patch("kzw.cli.remote_client.status", return_value={"missing": [B]}), \
                 patch("kzw.cli.remote_client.renew_lease"), \
                 patch("kzw.cli.remote_client.ensure_current_revision"), \
                 patch("kzw.cli.remote_client.publish") as publish:
                self.assertEqual(synchronize_exits(path, "/bin/kzw-pane", [B]), 1)
            self.assertEqual((path / "workspace.kitty-session").read_text(), EMPTY_SNAPSHOT)
            publish.assert_called_once_with(path, EMPTY_SNAPSHOT, [], confirmed_exits=True)
            query.assert_any_call(path / "kitty.sock", "close-window", "--match", "id:8")

    def test_server_hint_cannot_remove_live_session(self):
        current = tree()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "remote.json").write_text("{}")
            with patch("kzw.cli.query", return_value=json.dumps(current)) as query, \
                 patch("kzw.cli.remote_client.status", return_value={"missing": []}):
                self.assertEqual(synchronize_exits(path, "/bin/kzw-pane", [B]), 0)
            self.assertEqual(query.call_count, 1)

    def test_remote_detach_refuses_stale_revision_before_closing_view(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "remote.json").write_text("{}")
            with patch("kzw.cli.state_dir", return_value=path), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                 patch("kzw.cli.capture_stable_locked", return_value=1), \
                 patch("kzw.cli.remote_client.ensure_current_revision", side_effect=ValueError("revision changed")), \
                 patch("kzw.cli.query") as query:
                with self.assertRaisesRegex(ValueError, "revision changed"):
                    detach("Test", "origin")
            query.assert_not_called()

    def test_live_detached_session_is_not_removed(self):
        current = tree()
        current[0]["tabs"][0]["windows"][1]["user_vars"]["kzw_attach_ended"] = "1"
        with tempfile.TemporaryDirectory() as directory, \
             patch("kzw.cli.query", return_value=json.dumps(current)) as q, \
             patch("kzw.cli.zmx_command", return_value=B + "\n"):
            self.assertEqual(synchronize_exits(Path(directory), "/bin/kzw-pane"), 0)
            self.assertEqual(q.call_count, 1)


if __name__ == "__main__":
    unittest.main()
