import json
import shlex
import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch

from kzw.core import atomic_write, check_name, focused_pane, pane_map, rewrite_export, saved_focus
from kzw.cli import capture, detach, restore_focus
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
        with tempfile.TemporaryDirectory() as directory, patch("kzw.cli.query", side_effect=query):
            capture(Path(directory), "/bin/kzw-pane")
            text = (Path(directory) / "workspace.kitty-session").read_text()
            self.assertEqual(saved_focus(text), B)

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

    def test_missing_local_session_does_not_attach(self):
        result = __import__("subprocess").CompletedProcess(["zmx"], 0, stdout="another-session\n")
        with patch("sys.argv", ["kzw-pane", "--pane", A, "--host", "", "--existing"]), \
             patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO), \
             patch("builtins.input"), patch("kzw.pane.subprocess.run", return_value=result) as run:
            pane.main()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["zmx", "list", "--short"])

    def test_remote_restore_checks_existence_before_attach(self):
        result = __import__("subprocess").CompletedProcess(["ssh"], 0)
        with patch("sys.argv", ["kzw-pane", "--pane", A, "--host", "example-host", "--existing"]), \
             patch("sys.stdout", new_callable=io.StringIO), \
             patch("kzw.pane.subprocess.run", return_value=result) as run:
            pane.main()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["ssh", "-t", "example-host"])
        self.assertIn("zmx list --short", cmd[3])
        self.assertIn("exec zmx attach " + A, cmd[3])
        self.assertLess(cmd[3].index("zmx list"), cmd[3].index("exec zmx attach"))


if __name__ == "__main__":
    unittest.main()
