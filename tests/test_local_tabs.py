"""Local companion ownership, at-most-once startup, cleanup and view isolation."""

import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kzw import cli, local_tabs
from kzw.remote import Conflict, Store
from kzw.utility_tabs import only_utilities, workspace_tree
from workspace_fixtures import layout

A, B = ("kzw-" + c * 32 for c in "ab")
OWNER = "c" * 32


def local_window():
    return {"id": 9, "user_vars": {
        "kzw_local": B, "kzw_local_owner": OWNER,
        "kzw_local_workspace": "Test", "kzw_local_origin": "remote",
        "kzw_local_directory": "/state/Test",
        "kzw_local_source": "1", "kzw_local_source_pane": A}}


class LocalTabServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "server.sqlite")
        self.addCleanup(lambda: self.store.close())
        self.env = patch.dict(os.environ, {"KZW_LOCAL_COORDINATOR": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.live = []
        self.kill_fails = False
        self.list_fails = False

        def run(args, **kwargs):
            if args[1] == "kill":
                if self.kill_fails:
                    raise subprocess.CalledProcessError(1, args)
                self.live.remove(args[2])
            elif self.list_fails:
                raise OSError("listing unavailable")
            return subprocess.CompletedProcess(args, 0, "\n".join(self.live), "")

        self.run = patch("kzw.remote.subprocess.run", side_effect=run)
        self.run.start()
        self.addCleanup(self.run.stop)

    def request(self, action, **kwargs):
        return self.store.dispatch({"method": "local.tab." + action, "owner": OWNER, **kwargs})

    def reserve(self):
        return self.request("reserve", pane=B)

    def test_owned_inventory_and_no_remote_workspace_membership(self):
        self.reserve()
        self.live.append(B)
        inventory = self.store.inventory()
        session = inventory["sessions"][0]
        self.assertEqual(session["assignment"], "assigned")
        self.assertEqual(session["references"][0]["role"], "local-tab")
        self.assertEqual(inventory["workspaces"], {})
        self.assertEqual(self.store.dispatch({"method": "workspace.list"})["workspaces"], [])
        with self.assertRaisesRegex(Conflict, "local tab"):
            self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 0,
                                 "request_id": "claim", "panes": [B], "snapshot": layout(B)})

    def test_start_is_consumed_once_even_when_process_was_never_launched(self):
        from kzw.check import summarize
        self.reserve()
        self.assertEqual(summarize(self.store.inventory())["pending_sessions"], [B])
        self.assertTrue(self.request("launch", pane=B)["create"])
        self.assertEqual(summarize(self.store.inventory())["missing_sessions"], [B])
        with self.assertRaisesRegex(Conflict, "missing"):
            self.request("launch", pane=B)
        self.live.append(B)
        self.assertFalse(self.request("launch", pane=B)["create"])
        self.assertEqual(self.request("get")["tab"]["pane"], B)

    def test_uncertain_listing_does_not_consume_start_or_remove_layout(self):
        self.reserve()
        self.list_fails = True
        with self.assertRaises(OSError):
            self.request("launch", pane=B)
        self.assertEqual(self.request("get")["tab"]["state"], "reserved")
        with self.assertRaises(OSError):
            self.request("close", pane=B)
        self.assertEqual(self.request("get")["tab"]["state"], "closing")

    def test_durable_close_retries_exact_owned_id_and_keeps_unrelated_session(self):
        self.reserve()
        self.live = [A, B, "unassigned"]
        self.kill_fails = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.request("close", pane=B)
        self.assertEqual(self.request("get")["tab"]["state"], "closing")
        with self.assertRaisesRegex(Conflict, "cleanup"):
            self.request("launch", pane=B)
        with self.assertRaisesRegex(Conflict, "selection"):
            self.request("close", pane=A)
        self.kill_fails = False
        self.assertIsNone(self.request("close", pane=B)["tab"])
        self.assertEqual(self.live, [A, "unassigned"])

    def test_namespace_change_blocks_attachment_and_destruction(self):
        self.reserve()
        self.live.append(B)
        with patch.dict(os.environ, {"ZMX_DIR": "/different/namespace"}):
            for action in ("launch", "close"):
                with self.assertRaisesRegex(Conflict, "origin changed"):
                    self.request(action, pane=B)
        self.assertEqual(self.live, [B])

    def test_remote_coordinator_rejects_local_tab_requests(self):
        with patch.dict(os.environ, {"KZW_LOCAL_COORDINATOR": ""}):
            with self.assertRaisesRegex(ValueError, "local coordinator"):
                self.request("get")

    def test_rejects_adoption_collision_and_second_tab(self):
        self.live.append(B)
        with self.assertRaisesRegex(Conflict, "already exists"):
            self.reserve()
        self.live.clear()
        self.reserve()
        with self.assertRaisesRegex(Conflict, "already exists"):
            self.request("reserve", pane=A)
        with self.assertRaises(Conflict):
            self.store.dispatch({"method": "local.tab.reserve", "owner": "d" * 32, "pane": B})

    def test_focus_is_persistent_but_absence_does_not_prune(self):
        self.reserve()
        self.request("focus", pane=B, focused=True)
        self.store.inventory()
        self.assertTrue(self.request("get")["tab"]["focused"])
        self.assertEqual(self.request("get")["tab"]["pane"], B)

    def test_exit_requires_proof_and_never_kills(self):
        self.reserve()
        with patch.object(self.store, "exit_receipts", return_value={B: {}}) as receipts:
            self.assertEqual(self.request("exited", pane=B)["tab"]["state"], "reserved")
            receipts.assert_not_called()
        self.request("launch", pane=B)
        with patch.object(self.store, "exit_receipts", return_value={}):
            self.assertEqual(self.request("exited", pane=B)["tab"]["state"], "started")
        self.live = [A]
        with patch.object(self.store, "exit_receipts", return_value={B: {}}) as receipts:
            self.assertIsNone(self.request("exited", pane=B)["tab"])
            self.assertIsNone(self.request("exited", pane=B)["tab"])
            receipts.assert_called_once_with({B})
        self.assertEqual(self.live, [A])

    def test_confirmed_cleanup_intent_survives_coordinator_restart(self):
        self.reserve()
        self.live = [A, B]
        self.kill_fails = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.request("close", pane=B)
        path = Path(self.temp.name) / "server.sqlite"
        self.store.close()
        self.store = Store(path)
        self.kill_fails = False
        self.store.observe_detached()
        self.assertIsNone(self.request("get")["tab"])
        self.assertEqual(self.live, [A])

    def test_workspace_kill_retry_can_acquire_cleanup_lease_and_replay_original_revision(self):
        self.live = [A]
        self.store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 0,
                             "request_id": "create", "snapshot": layout(A), "panes": [A]})
        first, second = "d" * 32, "e" * 32
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": first})
        intent = {"method": "workspace.kill", "workspace": "Test", "revision": 1, "lease_token": first}
        self.store.dispatch(intent)
        self.store.dispatch({"method": "workspace.lease.release", "workspace": "Test", "token": first})
        self.store.dispatch({"method": "workspace.lease.acquire", "workspace": "Test", "token": second})
        result = self.store.dispatch({**intent, "lease_token": second})
        self.assertEqual(result["pending_cleanup"], [A])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(self.live, [A])  # Scheduling does not destroy terminals.


class LocalTabViewTests(unittest.TestCase):
    def tree(self):
        return [{"id": 7, "tabs": [
            {"id": 1, "is_active": False, "windows": [
                {"id": 1, "is_active": True, "user_vars": {"kzw_pane": A, "kzw_host": "remote"}}]},
            {"id": 2, "is_active": True, "windows": [local_window()]}]}]

    def test_projection_filters_only_explicit_singleton_and_preserves_source_focus(self):
        tree = self.tree()
        result = workspace_tree(tree)
        self.assertEqual(len(result[0]["tabs"]), 1)
        self.assertTrue(result[0]["tabs"][0]["is_active"])
        self.assertEqual(len(tree[0]["tabs"]), 2)
        for mutate in (
            lambda t: t[0]["tabs"][1]["windows"].append({"id": 11, "user_vars": {}}),
            lambda t: t[0]["tabs"][1]["windows"][0]["user_vars"].update(kzw_pane=A),
            lambda t: t[0]["tabs"][1]["windows"][0]["user_vars"].pop("kzw_local_owner"),
        ):
            bad = self.tree()
            mutate(bad)
            with self.assertRaisesRegex(ValueError, "local tab"):
                workspace_tree(bad)
        self.assertFalse(only_utilities({"tabs": [self.tree()[0]["tabs"][1]]}))

    def test_launch_is_local_sanitized_and_repeat_focuses_same_view(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "local-tab-owner").write_text(OWNER)
            tree = self.tree()
            tree[0]["tabs"].pop()
            tree[0]["tabs"][0]["windows"][0]["user_vars"].update(
                kzw_workspace="Test", kzw_origin="remote", kzw_directory=str(directory))
            state = {"tab": {"pane": B, "state": "started", "focused": True}}
            with patch.object(local_tabs, "windows", return_value=tree), \
                 patch.object(local_tabs, "request", return_value=state), \
                 patch.object(cli, "view_query", return_value="9") as query:
                local_tabs.launch(directory, "Test", "remote", target=1, focus=None)
                args = query.call_args.args
                self.assertIn("--env=KZW_RPC_SOCKET", args)
                self.assertIn("--env=KZW_WORKSPACE", args)
                self.assertIn("--var=kzw_local=" + B, args)
                self.assertNotIn("--var=kzw_pane=" + B, args)
                self.assertIn("_local-attach", args)
                self.assertNotIn("ssh", args)
                self.assertNotIn("--keep-focus", args)
                tree[0]["tabs"].append({"id": 2, "windows": [local_window()]})
                local_tabs.launch(directory, "Test", "remote", create=True)
                self.assertEqual(query.call_args.args[1], "focus-window")

    def test_local_title_monitor_uses_pinned_environment_and_reports_unknown(self):
        import threading
        stop = threading.Event()
        env = {"ZMX_DIR": "/pinned", "PATH": "/bin"}
        output = io.StringIO()
        def wait(_interval):
            if read.call_count == 3:
                stop.set()
        with patch("kzw.titles.read_process_details", side_effect=[
                {B: {"name": "bash"}}, {B: {"name": "yazi"}}, OSError("unavailable")]) as read, \
             patch.object(stop, "wait", side_effect=wait), patch("sys.stdout", output):
            local_tabs.monitor_title(B, env, stop, executable="/pinned/zmx")
        self.assertEqual(output.getvalue(), "\x1b]2;bash\x07\x1b]2;yazi\x07\x1b]2;?\x07")
        self.assertTrue(all(call.args == ("", env, "/pinned/zmx") for call in read.call_args_list))

    def test_no_local_binding_means_no_local_rpc_or_view_query(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(local_tabs, "windows") as windows, \
                patch.object(local_tabs.remote_client, "call") as rpc:
            local_tabs.launch(Path(temp), "Test")
            local_tabs.sync_focus(Path(temp))
            windows.assert_not_called()
            rpc.assert_not_called()

    def test_confirmed_close_checks_live_view_before_requesting_destruction(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "local-tab-owner").write_text(OWNER)
            with patch.object(local_tabs, "windows", return_value=self.tree()), \
                 patch.object(local_tabs, "request") as rpc, patch.object(cli, "view_query") as query:
                with self.assertRaisesRegex(ValueError, "selection changed"):
                    local_tabs.close(directory, A, 9)
                rpc.assert_not_called()
                local_tabs.close(directory, B, 9)
                rpc.assert_called_once_with(directory, "close", pane=B)
                query.assert_called_once_with(directory, "close-window", "--match", "id:9")

    def test_empty_workspace_can_detach_local_view_without_terminating_its_session(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "local-tab-owner").write_text(OWNER)
            (directory / "workspace.kitty-session").write_text(cli.EMPTY_SNAPSHOT)
            tree = self.tree()
            tree[0]["tabs"].pop(0)
            with patch.object(local_tabs, "windows", return_value=tree), \
                 patch.object(local_tabs, "sync_focus") as sync, \
                 patch.object(local_tabs, "request", return_value={"tab": {"pane": B}}) as rpc, \
                 patch.object(cli, "view_query") as query:
                self.assertTrue(local_tabs.detach_only(directory))
                sync.assert_called_once_with(directory)
                rpc.assert_called_once_with(directory)
                query.assert_called_once_with(directory, "close-window", "--match", "id:9")

    def test_save_cli_does_not_shadow_layout_capture_with_appearance_import(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("sys.argv", ["kzw", "save", "Test"]), \
             patch.object(cli, "state_dir", return_value=Path(temp)), \
             patch.object(cli, "capture", return_value=1) as capture, \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(), 0)
            capture.assert_called_once()

    def test_rename_recovers_interrupted_local_association_transfer(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_STATE_HOME": temp}):
            source = cli.state_dir("Test", "remote")
            target = cli.state_dir("Renamed", "remote")
            (source / "local-tab-owner").write_text(OWNER + "\n")
            original_write = cli.atomic_write
            dropped = False

            def write(path, text):
                nonlocal dropped
                if path == target / "local-tab-owner" and not dropped:
                    dropped = True
                    raise OSError("interrupted local association transfer")
                return original_write(path, text)

            def rpc(host, request):
                if request["method"] == "workspace.get":
                    return {"exists": True, "revision": 3, "panes": []}
                return {"name": "Renamed", "revision": 4}

            with patch.object(cli.remote_client, "call", side_effect=rpc), \
                 patch.object(cli, "atomic_write", side_effect=write):
                with self.assertRaises(OSError):
                    cli.rename_workspace("Test", "Renamed", "remote")
                self.assertTrue((source / "local-tab-owner").exists())
                cli.rename_workspace("Test", "Renamed", "remote")
            self.assertEqual((target / "local-tab-owner").read_text().strip(), OWNER)
            self.assertFalse((source / "local-tab-owner").exists())

    def test_confirmed_kill_lists_local_session_and_cleans_both_origins(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "local-tab-owner").write_text(OWNER)
            state = {"exists": True, "panes": [A], "revision": 3}
            calls = []

            def rpc(host, request):
                calls.append((host, request))
                if request["method"] == "workspace.get":
                    return state
                return {"remaining": []}

            with patch.object(cli, "state_dir", return_value=directory), \
                 patch.object(cli.remote_client, "call", side_effect=rpc), \
                 patch.object(cli.remote_client, "acquire_lease"), \
                 patch.object(cli.remote_client, "release_lease"), \
                 patch.object(cli.remote_client, "lease_token", return_value="token"), \
                 patch.object(local_tabs, "request", return_value={"tab": {"pane": B}}), \
                 patch.object(local_tabs, "close") as close, \
                 patch("builtins.input", return_value="y") as confirm:
                cli.kill_workspace("Test", "remote")
                self.assertIn(B, confirm.call_args.args[0])
                close.assert_called_once_with(directory, B)
                self.assertFalse((directory / "pending-local-kill.json").exists())
                self.assertEqual([r["method"] for _, r in calls],
                                 ["workspace.get", "workspace.kill", "cleanup.retry"])

    def test_failed_local_cleanup_retains_combined_kill_intent_after_remote_retirement(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "local-tab-owner").write_text(OWNER)
            state = {"exists": True, "panes": [A], "revision": 3}
            def rpc(host, request):
                return state if request["method"] == "workspace.get" else {"remaining": []}
            with patch.object(cli, "state_dir", return_value=directory), \
                 patch.object(cli.remote_client, "call", side_effect=rpc), \
                 patch.object(cli.remote_client, "acquire_lease"), \
                 patch.object(cli.remote_client, "release_lease"), \
                 patch.object(cli.remote_client, "lease_token", return_value="token"), \
                 patch.object(local_tabs, "request", return_value={"tab": {"pane": B}}), \
                 patch.object(local_tabs, "close", side_effect=OSError("unavailable")) as close, \
                 patch("builtins.input", return_value="y"):
                with self.assertRaises(OSError):
                    cli.kill_workspace("Test", "remote")
                journal = directory / "pending-local-kill.json"
                self.assertTrue(journal.exists())
                state.clear()
                state["exists"] = False
                close.side_effect = None
                cli.kill_workspace("Test", "remote")
                self.assertFalse(journal.exists())
                self.assertEqual(close.call_count, 2)


class LocalTabKittenTests(unittest.TestCase):
    def setUp(self):
        from test_kitty_adapters import load_adapter
        self.path = Path(__file__).resolve().parents[1] / "kzw/local_tab_kitten.py"
        self.module = load_adapter("local_kitten", self.path)
        self.window = SimpleNamespace(id=9, os_window_id=7, user_vars=local_window()["user_vars"])
        self.window.tabref = lambda: [self.window]
        self.boss = SimpleNamespace(window_id_map={9: self.window}, confirm=Mock(), show_error=Mock(),
                                    os_window_map={7: SimpleNamespace(
                                        tab_bar=SimpleNamespace(draw_func=lambda: None))})

    def test_close_confirmation_is_required_and_exact_local_identity_is_passed(self):
        with patch.object(self.module.subprocess, "Popen") as spawn:
            self.module.handle_result([str(self.path), "close"], None, 9, self.boss)
            spawn.assert_not_called()
            callback = self.boss.confirm.call_args.args[1]
            callback(False)
            spawn.assert_not_called()
            callback(True)
            command = spawn.call_args.args[0]
            self.assertIn("_local-tab", command)
            self.assertEqual(command[command.index("--close") + 1], B)
            self.assertEqual(command[command.index("--host") + 1], "remote")

    def test_old_attachment_is_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(self.module.subprocess, "Popen") as spawn:
            self.window.user_vars["kzw_local_directory"] = temp
            self.module.handle_result([str(self.path)], None, 9, self.boss)
            spawn.assert_not_called()
            self.assertIn("fresh attachment", self.boss.show_error.call_args.args[0])

    def test_open_reports_background_failure_only_after_exit(self):
        import sys
        timers = SimpleNamespace(add_timer=Mock(), remove_timer=Mock())

        def spawn(*args, stdout, **kwargs):
            stdout.write(b"kzw: local coordinator unavailable\n")
            return SimpleNamespace(poll=Mock(side_effect=[None, 1]))

        with patch.dict(sys.modules, {"kitty.fast_data_types": timers}), patch.object(
                self.module.subprocess, "Popen", side_effect=spawn):
            self.module.open_local(["kzw"], {}, self.boss)
            callback = timers.add_timer.call_args.args[0]
            callback(17)
            self.boss.show_error.assert_not_called()
            callback(17)
        timers.remove_timer.assert_called_once_with(17)
        self.boss.show_error.assert_called_once_with("Cannot open local tab", "kzw: local coordinator unavailable")

    def test_context_does_not_spawn_or_change_marker(self):
        self.window.current_marker_spec = ("function", "/privacy_marker.py")
        with patch.object(self.module.subprocess, "Popen") as spawn:
            result = self.module.handle_result([str(self.path), "--context"], None, 9, self.boss)
        self.assertEqual(json.loads(result)["marker"], ["function", "/privacy_marker.py"])
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
