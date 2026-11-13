"""Utility tabs must never become persistent shells or hide unmanaged work."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kzw import cli, utility
from kzw.core import focused_pane, pane_map
from kzw.utility_tabs import only_utilities, workspace_tree
from test_kitty_adapters import load_adapter

A, B = ("kzw-" + letter * 32 for letter in "ab")


def fixture(active=True):
    return [{"id": 42, "last_focused": True, "tabs": [
        {"id": 1, "is_active": not active, "windows": [
            {"id": 7, "is_active": True, "user_vars": {"kzw_pane": A, "kzw_host": "origin", "kzw_workspace": "Test"}}]},
        {"id": 3, "is_active": active, "windows": [
            {"id": 9, "is_active": True, "user_vars": {"kzw_utility": "condor-queue", "kzw_utility_source": "8",
                                                        "kzw_utility_pane": B}}]},
        {"id": 2, "is_active": False, "windows": [
            {"id": 8, "is_active": True, "user_vars": {"kzw_pane": B, "kzw_host": "origin", "kzw_workspace": "Test"}}]},
    ]}]


class UtilityTests(unittest.TestCase):
    def test_projection_preserves_order_and_underlying_focus_without_changing_input(self):
        for active, focus in ((True, B), (False, A)):
            with self.subTest(active=active):
                source = fixture(active)
                before = deepcopy(source)
                result = workspace_tree(source)
                self.assertEqual(source, before)
                self.assertEqual([tab["id"] for tab in result[0]["tabs"]], [1, 2])
                self.assertEqual(pane_map(result), {7: (A, "origin"), 8: (B, "origin")})
                self.assertEqual(focused_pane(result), focus)

    def test_missing_or_replaced_source_uses_a_retained_managed_tab(self):
        for mode in ("gone", "replaced"):
            source = fixture()
            if mode == "gone":
                source[0]["tabs"].pop()
            else:
                source[0]["tabs"][-1]["windows"][0]["user_vars"]["kzw_pane"] = A
            result = workspace_tree(source)
            self.assertEqual(focused_pane(result), A)

    def test_invalid_and_mixed_utility_tabs_fail_closed(self):
        for mode in ("managed", "split", "unknown", "bad source", "missing identity"):
            with self.subTest(mode=mode):
                source = fixture()
                tab = source[0]["tabs"][1]
                values = tab["windows"][0]["user_vars"]
                if mode == "managed":
                    values["kzw_pane"] = A
                elif mode == "split":
                    tab["windows"].append({"id": 10, "user_vars": {}})
                elif mode == "unknown":
                    values["kzw_utility"] = "unknown"
                elif mode == "bad source":
                    values["kzw_utility_source"] = "-1"
                else:
                    values.pop("kzw_utility_pane")
                with self.assertRaisesRegex(ValueError, "utility tab"):
                    workspace_tree(source)

    def test_unmarked_unmanaged_tabs_are_not_hidden(self):
        source = fixture()
        source[0]["tabs"][1]["windows"][0]["user_vars"] = {}
        result = workspace_tree(source)
        self.assertEqual(len(result[0]["tabs"]), 3)
        with self.assertRaisesRegex(ValueError, "not managed"):
            pane_map(result)

    def test_utility_only_window_does_not_keep_workspace_attached(self):
        source = fixture()
        source[0]["tabs"] = source[0]["tabs"][1:2]
        self.assertTrue(only_utilities(source[0]))
        self.assertEqual(workspace_tree(source), [])
        view = {"address": "unix:/test.sock", "os_window_id": 42, "panes": [A, B]}
        with patch("kzw.cli.list_os_windows", return_value=source):
            self.assertFalse(cli.view_alive(Path("/unused"), view))

    def test_query_filters_only_utility_tabs_and_saves_source_focus(self):
        reply = subprocess.CompletedProcess([], 0, json.dumps(fixture()), "")
        with patch("kzw.cli.subprocess.run", return_value=reply):
            result = json.loads(cli.query("/test.sock", "ls", workspace_os_id=42))
        self.assertEqual(focused_pane(result), B)
        self.assertEqual(set(pane_map(result)), {7, 8})

    def test_scoped_snapshot_filters_windows_and_corrects_focus_index(self):
        module = load_adapter("scoped_session")
        tree = fixture()
        tree[0]["tabs"][1]["windows"][0]["user_vars"].update(kzw_utility_source="7", kzw_utility_pane=A)
        windows = {w["id"]: Mock(id=w["id"], os_window_id=42, user_vars=w["user_vars"])
                   for tab in tree[0]["tabs"] for w in tab["windows"]}
        manager = Mock(tabs=[[windows[w["id"]] for w in tab["windows"]] for tab in tree[0]["tabs"]])
        manager.serialize_state_as_session.return_value = ["new_tab First", "new_tab Second", "focus_tab 1"]
        boss = Mock(window_id_map=windows, os_window_map={42: manager})
        boss.list_os_windows.return_value = iter(tree)
        result = json.loads(module.handle_result(["kitten", "42", "--with-tree"], None, 7, boss))
        self.assertEqual(result["tree"], workspace_tree(tree))
        self.assertEqual(manager.serialize_state_as_session.call_args.args[1], frozenset([windows[7], windows[8]]))
        self.assertEqual(result["session"], "new_tab First\nnew_tab Second\nfocus_tab 0")
        self.assertEqual(focused_pane(result["tree"]), A)

    def test_queue_launch_uses_managed_creation_and_selected_context(self):
        module = load_adapter("utility")
        for host in ("", "origin"):
            window = SimpleNamespace(id=7, os_window_id=42, user_vars={"kzw_workspace": "Test", "kzw_pane": A,
                                                                     "kzw_host": host, "kzw_origin": host},
                                     child=SimpleNamespace(environ={"KZW_RPC_SOCKET": "/selected/broker"}))
            boss = Mock(window_id_map={7: window})
            with patch.dict(os.environ, {"KZW_HOST": "wrong", "KZW_RPC_SOCKET": "/wrong/broker"}), \
                 patch.object(module.shutil, "which", return_value="/bin/kzw"), \
                 patch.object(module.subprocess, "Popen") as popen:
                module.handle_result(["kitten"], None, 7, boss)
            command = popen.call_args.args[0]
            self.assertEqual(command[:10], ["/bin/kzw", "_managed-create", "Test", "tab", "7", A,
                                            "--host", host, "--initial-program", "condor-queue"])
            self.assertEqual(command[10:], ["--origin", host] if host else [])
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["KZW_RPC_SOCKET"], "/selected/broker")
            self.assertEqual(env["KZW_WORKSPACE_ORIGIN"], host)
            boss.call_remote_control.assert_not_called()
            boss.show_error.assert_not_called()

    def test_unmanaged_and_bad_host_fail_without_launch_and_utility_does_not_nest(self):
        module = load_adapter("utility")
        for values in ({}, {"kzw_workspace": "Test", "kzw_pane": A, "kzw_host": "-oProxyCommand=bad"},
                       {"kzw_utility": "condor-queue"}):
            boss = Mock(window_id_map={7: SimpleNamespace(user_vars=values)})
            with patch.object(module.subprocess, "Popen") as popen:
                module.handle_result(["kitten"], None, 7, boss)
            popen.assert_not_called()
            boss.call_remote_control.assert_not_called()

    def test_utility_close_is_not_a_managed_session_close(self):
        module = load_adapter("close")
        window = SimpleNamespace(tab_id=3, user_vars={"kzw_utility": "condor-queue"})
        boss = Mock(window_id_map={9: window})
        boss.tab_for_id.return_value = [window]
        with patch.object(module.subprocess, "Popen") as popen:
            module.handle_result(["kitten", "tab"], None, 9, boss)
        boss.mark_window_for_close.assert_called_once_with(window)
        boss.confirm.assert_not_called()
        popen.assert_not_called()
        boss.mark_window_for_close.reset_mock()
        boss.tab_for_id.return_value = [window, SimpleNamespace(user_vars={"kzw_pane": A})]
        module.handle_result(["kitten", "tab"], None, 9, boss)
        boss.mark_window_for_close.assert_not_called()

    def test_remote_command_uses_tty_login_environment_and_home_on_origin(self):
        command = utility.queue_command("origin")
        self.assertEqual(command[:-1], ["ssh", "-t", "-o", "ConnectTimeout=10", "--", "origin"])
        remote = shlex.split(command[-1])
        self.assertEqual(remote[:2], ["bash", "-lc"])
        self.assertIn('$HOME/dotfiles/scripts/tmux_condor_queue.sh', remote[2])
        self.assertEqual(utility.queue_command("")[:2], ["bash", "-lc"])
        for host in ("-bad", "origin;bad", "origin bad"):
            with self.assertRaises(ValueError):
                utility.queue_command(host)

    def test_noninteractive_shell_finds_home_fzf_without_overriding_existing_install(self):
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        for mode in ("fallback", "existing", "missing", "not executable"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="kzw queue ' ") as tmp:
                home = Path(tmp)
                tools, fzf_dir = home / ".local/bin", home / ".fzf/bin"
                picker = home / "dotfiles/scripts/tmux_condor_queue.sh"
                tools.mkdir(parents=True)
                fzf_dir.mkdir(parents=True)
                picker.parent.mkdir(parents=True)
                picker.write_text('exec fzf\n')
                (tools / "bash").symlink_to(bash)
                for name in ("condor_q", "less", "date"):
                    tool = tools / name
                    tool.write_text('#!/bin/sh\nexit 99\n')  # Existence checks only; never query a real queue.
                    tool.chmod(0o700)
                if mode != "missing":
                    fzf = fzf_dir / "fzf"
                    fzf.write_text('#!/bin/sh\nprintf "fallback-fzf\\n"\n')
                    fzf.chmod(0o600 if mode == "not executable" else 0o700)
                if mode == "existing":
                    fzf = tools / "fzf"
                    fzf.write_text('#!/bin/sh\nprintf "existing-fzf\\n"\n')
                    fzf.chmod(0o700)
                local_script = utility.queue_command("")[2]
                remote_script = shlex.split(utility.queue_command("origin")[-1])[2]
                self.assertEqual(local_script, remote_script)
                result = subprocess.run([bash, "-c", local_script], env={"HOME": str(home), "PATH": "/nonexistent"},
                                        capture_output=True, text=True, timeout=5)
                if mode in ("missing", "not executable"):
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("Required command not found: fzf", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, ("existing" if mode == "existing" else "fallback") + "-fzf\n")

    def test_runner_clears_workspace_environment_and_keeps_errors_visible_without_retry(self):
        for status in (0, 255):
            with patch.dict(os.environ, {"KZW_WORKSPACE": "Wrong", "ZMX_SESSION": A, "TERM": "xterm-kitty"}, clear=True), \
                 patch("kzw.utility.subprocess.run", return_value=SimpleNamespace(returncode=status)) as run, \
                 patch("builtins.input", return_value="") as wait, patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(utility.run_queue("origin"), status)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["env"], {"TERM": "xterm-kitty"})
            self.assertEqual(wait.call_count, int(bool(status)))
            if status:
                self.assertIn("255", error.getvalue())

    def test_creation_is_refused_inside_a_utility_tab(self):
        module = load_adapter("create")
        boss = Mock(window_id_map={9: SimpleNamespace(user_vars={"kzw_utility": "condor-queue"})})
        with patch.object(module.subprocess, "Popen") as popen:
            module.handle_result(["kitten", "tab"], None, 9, boss)
        popen.assert_not_called()
        boss.show_error.assert_called_once()

    def test_queue_cli_does_not_query_or_mutate_workspace_state(self):
        with patch("sys.argv", ["kzw", "_queue", "--host", "origin"]), \
             patch("kzw.utility.run_queue", return_value=0) as run, patch("kzw.cli.remote_client.call") as rpc:
            self.assertEqual(cli.main(), 0)
        run.assert_called_once_with("origin")
        rpc.assert_not_called()


DOTFILES = Path(os.environ.get("KZW_TEST_DOTFILES", Path.home() / "dotfiles"))


@unittest.skipUnless((DOTFILES / ".config/kitty/helpers/kzw_close.py").is_file(), "optional dotfiles helpers unavailable")
class DotfilesUtilityTests(unittest.TestCase):
    def setUp(self):
        self.module = load_adapter("dotfiles_close", DOTFILES / ".config/kitty/helpers/kzw_close.py")
        self.source = SimpleNamespace(id=7, os_window_id=42, user_vars={
            "kzw_pane": A, "kzw_host": "origin", "kzw_workspace": "Test", "kzw_origin": "origin"})
        self.queue = SimpleNamespace(id=9, os_window_id=42, user_vars={"kzw_utility": "condor-queue"})
        self.tab = [self.queue]
        self.queue.tabref = lambda: self.tab
        self.manager = SimpleNamespace(tabs=[[self.source], self.tab])
        self.boss = Mock(window_id_map={7: self.source, 9: self.queue}, os_window_map={42: self.manager})

    def test_cmd_w_closes_just_the_utility_without_a_session_kill(self):
        with patch.object(self.module.subprocess, "Popen") as popen:
            self.module.handle_result(["kitten", "pane"], None, 9, self.boss)
        self.boss.mark_window_for_close.assert_called_once_with(self.queue)
        self.boss.confirm.assert_not_called()
        popen.assert_not_called()

    def test_detach_confirmation_ignores_only_singleton_utility_tabs(self):
        with patch.object(self.module.subprocess, "Popen") as popen:
            self.module.handle_result(["kitten", "os-window"], None, 7, self.boss)
            self.boss.confirm.call_args.args[1](True)
        command = popen.call_args.args[0]
        self.assertEqual(command[1:], ["detach", "Test", "--host", "origin"])
        self.boss.mark_window_for_close.assert_not_called()
        self.tab.append(SimpleNamespace(id=10, user_vars={}))
        self.boss.reset_mock()
        self.module.handle_result(["kitten", "os-window"], None, 7, self.boss)
        self.boss.confirm.assert_not_called()
        self.boss.show_error.assert_called_once()

    def test_utility_does_not_create_unmanaged_splits_or_tabs(self):
        module = load_adapter("dotfiles_create", DOTFILES / ".config/kitty/helpers/kzw_new_tab.py")
        for kind in ("tab", "split", "right", "below"):
            with self.subTest(kind=kind):
                module.handle_result(["kitten", kind], None, 9, self.boss)
        self.boss.call_remote_control.assert_not_called()
        self.assertEqual(self.boss.show_error.call_count, 4)


if __name__ == "__main__":
    unittest.main()
