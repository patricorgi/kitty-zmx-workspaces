"""Unit coverage for same-instance attach and background supervision.

These tests never start Kitty, SSH or zmx. Real lifecycle coverage lives in
the opt-in integration modules.
"""

import argparse
import fcntl
import io
import itertools
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from kzw import cli, kitty_instance
from kzw.core import FLAG
from workspace_fixtures import layout

PANE_A = "kzw-" + "a" * 32
PANE_B = "kzw-" + "b" * 32


def view_window(os_id, panes, last_focused=False):
    return {"id": os_id, "last_focused": last_focused,
            "tabs": [{"id": os_id, "is_active": True,
                      "windows": [{"id": os_id * 10 + i, "is_active": i == 0,
                                   "user_vars": {"kzw_pane": pane, "kzw_host": ""}}
                                  for i, pane in enumerate(panes)]}]}


class SocketGroupTests(unittest.TestCase):
    def test_group_is_read_only_from_an_exact_socket_name(self):
        self.assertEqual(kitty_instance.group_from_socket("/run/user/501/kitty-ipc-501", 501), "")
        self.assertEqual(kitty_instance.group_from_socket("/run/user/501/kitty-ipc-501-main", 501), "main")
        self.assertEqual(kitty_instance.group_from_socket("/run/user/501/kitty-ipc-501-a.b@c", 501), "a.b@c")
        for path in ("/run/user/501/kitty-ipc-999", "/run/user/501/kitty-ipc-501-",
                     "/run/user/501/kitty-ipc-501-bad/slash", "/run/user/501/other"):
            self.assertIsNone(kitty_instance.group_from_socket(path, 501))

    def test_single_instance_group_scans_lsof_output(self):
        result = subprocess.CompletedProcess(
            [], 0, "p123\nn/run/user/501/kitty-ipc-999\nn/run/user/501/kitty-ipc-501-work\n", "")
        with patch("kzw.kitty_instance.subprocess.run", return_value=result) as run:
            self.assertEqual(kitty_instance.single_instance_group(123, 501), "work")
        run.assert_called_once_with(["lsof", "-a", "-p", "123", "-U", "-Fn"],
                                    capture_output=True, text=True, timeout=5)

    def test_single_instance_group_fails_closed(self):
        for result in (subprocess.CompletedProcess([], 1, "", ""),
                       subprocess.CompletedProcess([], 0, "n/elsewhere\n", "")):
            with patch("kzw.kitty_instance.subprocess.run", return_value=result):
                self.assertIsNone(kitty_instance.single_instance_group(123, 501))
        with patch("kzw.kitty_instance.subprocess.run", side_effect=OSError("no lsof")):
            self.assertIsNone(kitty_instance.single_instance_group(123, 501))
        with self.assertRaises(ValueError):
            kitty_instance.single_instance_group(0)


class ContextTests(unittest.TestCase):
    def test_context_requires_kitty_identity_and_control(self):
        with patch("kzw.kitty_instance.single_instance_group", return_value="main"):
            context = kitty_instance.context({"KITTY_PID": "123", "KITTY_LISTEN_ON": "unix:/tmp/x"})
        self.assertEqual(context, {"pid": 123, "address": "unix:/tmp/x", "group": "main"})
        for env in ({}, {"KITTY_PID": "abc", "KITTY_LISTEN_ON": "unix:/tmp/x"},
                    {"KITTY_PID": "123", "KITTY_LISTEN_ON": "/tmp/x"},
                    {"KITTY_PID": "123"}):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, "new-kitty-instance"):
                kitty_instance.context(env)

    def test_context_rejects_instances_without_a_single_instance_socket(self):
        with patch("kzw.kitty_instance.single_instance_group", return_value=None), \
             self.assertRaisesRegex(ValueError, "--single-instance"):
            kitty_instance.context({"KITTY_PID": "123", "KITTY_LISTEN_ON": "unix:/tmp/x"})

    def test_probe_reports_unavailable_remote_control(self):
        with patch("kzw.kitty_instance.os_windows", return_value=[{"id": 1}]):
            kitty_instance.probe("unix:/tmp/x")
        with patch("kzw.kitty_instance.os_windows", side_effect=OSError("refused")), \
             self.assertRaisesRegex(ValueError, "allow_remote_control"):
            kitty_instance.probe("unix:/tmp/x")

    def test_launch_command_preserves_the_instance_group(self):
        self.assertEqual(kitty_instance.launch_command("", "/tmp/s"),
                         ["kitty", "--single-instance", "--session", "/tmp/s"])
        self.assertEqual(kitty_instance.launch_command("main", "/tmp/s"),
                         ["kitty", "--single-instance", "--instance-group", "main",
                          "--session", "/tmp/s"])


class ViewStateTests(unittest.TestCase):
    def test_view_state_round_trips_and_rejects_unsafe_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertIsNone(cli.view_state(directory))
            cli.write_view(directory, "unix:/tmp/sock", 7, 42, [PANE_B, PANE_A])
            self.assertEqual(cli.view_state(directory), {
                "address": "unix:/tmp/sock", "instance_pid": 7,
                "os_window_id": 42, "panes": [PANE_A, PANE_B]})
            self.assertTrue((directory / "kitty.sock").exists())

            (directory / "view.json").write_text(json.dumps({
                "address": "/tmp/sock", "instance_pid": 7, "os_window_id": 42,
                "panes": [PANE_A]}))
            with self.assertRaisesRegex(ValueError, "invalid workspace view state"):
                cli.view_state(directory)

            (directory / "view.json").unlink()
            (directory / "view.json").symlink_to(directory / "missing")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                cli.view_state(directory)

            cli.remove_view(directory)
            self.assertFalse((directory / "view.json").exists() or (directory / "kitty.sock").exists())

    def test_view_query_scopes_to_the_recorded_os_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch("kzw.cli.query", return_value="[]") as query:
                cli.view_query(directory, "ls")
            query.assert_called_once_with(directory / "kitty.sock", "ls")

            cli.write_view(directory, "unix:/tmp/sock", 7, 42, [PANE_A])
            with patch("kzw.cli.query", return_value="[]") as query:
                cli.view_query(directory, "ls")
            query.assert_called_once_with("unix:/tmp/sock", "ls", workspace_os_id=42)

    def test_view_alive_requires_the_exact_window_and_a_managed_pane(self):
        view = {"address": "unix:/tmp/sock", "instance_pid": 7,
                "os_window_id": 42, "panes": [PANE_A]}
        with tempfile.TemporaryDirectory():
            with patch("kzw.cli.list_os_windows", return_value=[view_window(42, [PANE_A])]):
                self.assertTrue(cli.view_alive(Path("."), view))
            with patch("kzw.cli.list_os_windows", return_value=[view_window(42, [PANE_B])]), \
                 self.assertRaisesRegex(ValueError, "uncertain view"):
                cli.view_alive(Path("."), view)
            with patch("kzw.cli.list_os_windows", return_value=[view_window(43, [PANE_A])]):
                self.assertFalse(cli.view_alive(Path("."), view))
            with patch("kzw.cli.list_os_windows", side_effect=OSError("gone")), \
                 patch("kzw.cli.os.kill", side_effect=ProcessLookupError):
                self.assertFalse(cli.view_alive(Path("."), view))
            with patch("kzw.cli.list_os_windows", side_effect=OSError("timeout")), \
                 patch("kzw.cli.os.kill"), \
                 self.assertRaisesRegex(ValueError, "cannot verify"):
                cli.view_alive(Path("."), view)
            self.assertFalse(cli.view_alive(Path("."), None))


class SessionContextTests(unittest.TestCase):
    def test_workspace_context_keeps_env_and_window_vars_in_sync(self):
        env, variables = cli.workspace_context(
            "Test", Path("/state/Test"), "origin", True, rpc_socket="/tmp/rpc",
            marker=[["bold", "true"]])
        self.assertEqual(env["KZW_WORKSPACE"], "Test")
        self.assertEqual(env["KZW_WORKSPACE_ORIGIN"], "origin")
        self.assertEqual(env["KZW_WORKSPACE_COORDINATOR"], "1")
        self.assertEqual(env["KZW_RPC_SOCKET"], "/tmp/rpc")
        self.assertEqual(env["KZW_INHERIT_MARKER"], '[["bold","true"]]')
        self.assertEqual(variables["kzw_workspace"], "Test")
        self.assertEqual(variables["kzw_origin"], "origin")
        self.assertEqual(variables["kzw_marker"], '[["bold","true"]]')
        self.assertEqual(variables["kzw_signal"], "/state/Test/watcher.changed")

        local, local_vars = cli.workspace_context("Test", Path("/state/Test"), "other")
        self.assertEqual(local["KZW_WORKSPACE_ORIGIN"], "")
        self.assertNotIn("KZW_RPC_SOCKET", local)
        self.assertNotIn("kzw_marker", local_vars)

    def test_inject_session_context_only_rewrites_launch_lines(self):
        text = ("new_tab Test\n"
                "launch " + FLAG + '{"id":1}' + " --var=keep=1 " + PANE_A + "\n"
                "launch " + FLAG + '{"id":2}' + " " + PANE_B + "\n")
        rendered = cli.inject_session_context(
            text, {"KZW_WORKSPACE": "Test"}, {"kzw_workspace": "Test"})
        lines = rendered.splitlines()
        self.assertEqual(lines[0], "new_tab Test")
        args = shlex.split(lines[1])
        self.assertEqual(args[0], "launch")
        self.assertTrue(args[1].startswith(FLAG))
        self.assertIn("--var=kzw_workspace=Test", args)
        self.assertIn("--env=KZW_WORKSPACE=Test", args)
        self.assertIn("--var=keep=1", args)
        self.assertEqual(args[-1], PANE_A)
        self.assertTrue(rendered.endswith("\n"))

    def test_session_panes_rejects_missing_or_duplicate_identities(self):
        text = ("launch " + FLAG + '{"id":1}' + " --pane " + PANE_A + "\n"
                "launch " + FLAG + '{"id":2}' + " --pane " + PANE_B + "\n")
        self.assertEqual(cli.session_panes(text), {PANE_A, PANE_B})
        for bad in ("new_tab Test\n",
                    "launch --pane " + PANE_A + "\nlaunch --pane " + PANE_A + "\n",
                    "launch --pane kzw-short\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cli.session_panes(bad)

    def test_write_runtime_session_leaves_the_snapshot_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "workspace.kitty-session"
            source.write_text("launch " + FLAG + '{"id":1}' + " --pane " + PANE_A + "\n")
            runtime, panes = cli.write_runtime_session(
                directory, source, {"KZW_WORKSPACE": "Test"}, {"kzw_workspace": "Test"})
            try:
                self.assertEqual(panes, {PANE_A})
                self.assertNotEqual(runtime, source)
                self.assertIn("--env=KZW_WORKSPACE=Test", runtime.read_text())
                self.assertNotIn("--env=KZW_WORKSPACE=Test", source.read_text())
            finally:
                runtime.unlink()


class WindowIdentificationTests(unittest.TestCase):
    def test_identify_os_window_requires_exactly_one_new_window(self):
        before = {1}
        with patch("kzw.cli.list_os_windows", return_value=[view_window(1, [PANE_B]),
                                                            view_window(2, [PANE_A])]), \
             patch("kzw.cli.time.monotonic", return_value=0), \
             patch("kzw.cli.time.sleep"):
            self.assertEqual(cli.identify_os_window("unix:/tmp/sock", before, {PANE_A}), 2)

        with patch("kzw.cli.list_os_windows", return_value=[view_window(2, [PANE_A]),
                                                            view_window(3, [PANE_A])]), \
             patch("kzw.cli.time.monotonic", return_value=0), \
             patch("kzw.cli.time.sleep"), \
             self.assertRaisesRegex(ValueError, "multiple"):
            cli.identify_os_window("unix:/tmp/sock", before, {PANE_A})

        with patch("kzw.cli.list_os_windows", return_value=[view_window(1, [PANE_B])]), \
             patch("kzw.cli.time.monotonic", side_effect=[0, 100]), \
             patch("kzw.cli.time.sleep"), \
             self.assertRaisesRegex(ValueError, "did not create"):
            cli.identify_os_window("unix:/tmp/sock", before, {PANE_A})


class TransientExportTests(unittest.TestCase):
    def test_rewrite_export_strips_only_per_view_metadata(self):
        export = shlex.join([
            "launch", FLAG + json.dumps({"id": 1}),
            "--var=kzw_workspace=Test", "--var=kzw_marker=m", "--var=keep=1",
            "--env=KZW_WORKSPACE=Test", "--env=OTHER=1", "--cwd=/tmp",
            "/bin/shell"]) + "\n"
        rewritten = cli.rewrite_export(export, {1: (PANE_A, "")}, "/bin/kzw-pane")
        args = shlex.split(rewritten.splitlines()[0])
        self.assertNotIn("--var=kzw_workspace=Test", args)
        self.assertNotIn("--var=kzw_marker=m", args)
        self.assertNotIn("--env=KZW_WORKSPACE=Test", args)
        self.assertIn("--var=keep=1", args)
        self.assertIn("--env=OTHER=1", args)
        self.assertIn("--cwd=/tmp", args)
        self.assertEqual(args[2:], ["--var=keep=1", "--env=OTHER=1", "--cwd=/tmp",
                                    "/bin/kzw-pane", "--pane", PANE_A, "--host", "",
                                    "--existing"])


class BackgroundSupervisorTests(unittest.TestCase):
    def args(self, command="start", name="Test"):
        return argparse.Namespace(command=command, name=name, host="", attach=False)

    def test_background_routing_covers_only_attaching_commands(self):
        self.assertTrue(cli.background_command(self.args("start")))
        self.assertTrue(cli.background_command(self.args("attach")))
        self.assertTrue(cli.background_command(self.args("new-pane")))
        self.assertFalse(cli.background_command(self.args("create")))
        attached = self.args("create")
        attached.attach = True
        self.assertTrue(cli.background_command(attached))
        self.assertFalse(cli.background_command(self.args("detach")))

    def test_spawn_background_reports_startup_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            args = self.args()
            child = Mock()
            child.poll.return_value = None

            def finish(_delay):
                (directory / "supervisor.startup.json").write_text('{"ok": true}\n')

            with patch("kzw.cli.background_directory", return_value=directory), \
                 patch("kzw.cli.subprocess.Popen", return_value=child) as popen, \
                 patch("kzw.cli.time.sleep", side_effect=finish):
                self.assertEqual(cli.spawn_background(args), 0)
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["KZW_BACKGROUND_SUPERVISOR"], "1")
            self.assertEqual(env["KZW_STARTUP_STATUS"], str(directory / "supervisor.startup.json"))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertFalse((directory / "supervisor.startup.json").exists())

    def test_spawn_background_reports_a_startup_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)

            def fail(_argv, **kwargs):
                (directory / "supervisor.startup.json").write_text('{"ok": false, "error": "boom"}\n')
                return Mock()

            with patch("kzw.cli.background_directory", return_value=directory), \
                 patch("kzw.cli.subprocess.Popen", side_effect=fail), \
                 patch("kzw.cli.time.sleep"):
                self.assertEqual(cli.spawn_background(self.args()), 1)
            self.assertFalse((directory / "supervisor.startup.json").exists())

    def test_spawn_background_reports_early_exit_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            child = Mock(returncode=9)
            child.poll.return_value = 9
            with patch("kzw.cli.background_directory", return_value=directory), \
                 patch("kzw.cli.subprocess.Popen", return_value=child), \
                 patch("kzw.cli.time.sleep"):
                self.assertEqual(cli.spawn_background(self.args()), 9)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch("kzw.cli.background_directory", return_value=directory), \
                 patch("kzw.cli.subprocess.Popen", return_value=Mock()), \
                 patch("kzw.cli.time.monotonic", side_effect=[0, 200]), \
                 patch("kzw.cli.time.sleep"):
                self.assertEqual(cli.spawn_background(self.args()), 1)

    def test_spawn_background_refuses_an_unsafe_status_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "supervisor.startup.json").symlink_to(directory / "other")
            with patch("kzw.cli.background_directory", return_value=directory), \
                 patch("kzw.cli.subprocess.Popen") as popen, \
                 self.assertRaisesRegex(ValueError, "unsafe"):
                cli.spawn_background(self.args())
            popen.assert_not_called()

    def test_spawn_background_refuses_unsafe_log_before_launch(self):
        for kind in ("symlink", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                log = directory / "supervisor.log"
                if kind == "symlink":
                    log.symlink_to(directory / "missing")
                else:
                    log.mkdir()
                with patch("kzw.cli.background_directory", return_value=directory), \
                     patch("kzw.cli.subprocess.Popen") as popen, self.assertRaisesRegex(ValueError, "unsafe supervisor log"):
                    cli.spawn_background(self.args())
                popen.assert_not_called()

    def test_cli_attachment_flags_choose_background_or_foreground_execution(self):
        cases = ((["attach", "Test"], "background", {}),
                 (["attach", "Test", "--hold"], "attach", {}),
                 (["attach", "Test", "--hold", "--new-kitty-instance"], "attach", {"new_instance": True}),
                 (["create", "Test", "--attach"], "background", {}),
                 (["create", "Test", "--attach", "--hold"], "attach", {}),
                 (["create", "Test"], "create", {}))
        for argv, expected, kwargs in cases:
            with self.subTest(argv=argv), patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["kzw", *argv]), \
                 patch("kzw.cli.spawn_background", return_value=0) as background, \
                 patch("kzw.cli.attach_remote", return_value=0) as attach, patch("kzw.cli.create_remote") as create:
                self.assertEqual(cli.main(), 0)
                if expected == "background":
                    background.assert_called_once()
                    attach.assert_not_called()
                    create.assert_not_called()
                elif expected == "attach":
                    background.assert_not_called()
                    attach.assert_called_once_with("Test", "", **kwargs)
                    self.assertEqual(create.call_count, int(argv[0] == "create"))
                else:
                    create.assert_called_once_with("Test", "")
                    attach.assert_not_called()
                    background.assert_not_called()


class SharedSupervisorTests(unittest.TestCase):
    def test_shared_supervisor_preserves_state_across_restore_bootstrap_rejection_and_disconnect(self):
        class EndTest(BaseException):
            pass

        for mode in ("restore", "bootstrap", "launch-rejected", "disconnected"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, ExitStack() as patches:
                directory = Path(tmp)
                source = directory / ("bootstrap.kitty-session" if mode == "bootstrap" else "workspace.kitty-session")
                original = layout(PANE_A)
                source.write_text(original)
                (directory / "remote.json").write_text("{}")
                events, launched = [], []
                state = {"gone": False, "ready": mode != "bootstrap", "sleeps": 0}
                title_stop, status_stop, broker = Mock(), Mock(), Mock(path=directory / "rpc.sock")
                context = {"pid": 123, "address": "unix:/test/kitty", "group": "test-instance"}
                utility = view_window(1, [PANE_B])

                def windows(_address):
                    if not launched:
                        return [utility]
                    if (directory / "view.json").exists() and state["gone"]:
                        if mode == "disconnected":
                            raise OSError("temporary Kitty control failure")
                        return [utility]
                    return [utility, view_window(42, [PANE_A])]

                def launch(command, **kwargs):
                    runtime = Path(command[command.index("--session") + 1])
                    launched.append((command, runtime, runtime.read_text()))
                    return subprocess.CompletedProcess(command, 1 if mode == "launch-rejected" else 0, "", "launch rejected")

                def finish(*args):
                    if not events:
                        events.append("bootstrap deferred")
                        raise ValueError("registration not ready")
                    state["ready"] = True
                    events.append("bootstrap ready")

                def acknowledge(*args, **kwargs):
                    self.assertTrue(state["ready"], "startup was acknowledged before bootstrap finished")
                    events.append("ready")

                def capture(*args):
                    events.append("capture")
                    state["gone"] = True

                def sleep(_delay):
                    state["sleeps"] += 1
                    if mode == "disconnected" and state["sleeps"] >= 4:
                        raise EndTest()
                    self.assertLess(state["sleeps"], 20, "supervisor failed to release a vanished view")

                replacements = {
                    "state_dir": {"return_value": directory}, "shutil.which": {"return_value": "/bin/kzw-pane"},
                    "kitty_instance.context": {"return_value": context}, "kitty_instance.probe": {},
                    "kitty_instance.tab_style": {},
                    "list_os_windows": {"side_effect": windows}, "start_title_monitor": {"return_value": title_stop},
                    "remote_client.start_status_monitor": {"return_value": status_stop},
                    "remote_client.ConnectionBroker": {"return_value": broker}, "mark_bootstrap_launching": {},
                    "finish_bootstrap": {"side_effect": finish}, "startup_status": {"side_effect": acknowledge},
                    "capture": {"side_effect": capture}, "subprocess.run": {"side_effect": launch},
                    "subprocess.Popen": {"side_effect": AssertionError("shared attachment must not spawn a private Kitty")},
                    "time.monotonic": {"side_effect": itertools.count().__next__}, "time.sleep": {"side_effect": sleep},
                }
                mocks = {target: patches.enter_context(patch("kzw.cli." + target, **kwargs)) for target, kwargs in replacements.items()}
                patches.enter_context(patch.dict(os.environ, {}, clear=True))
                patches.enter_context(patch("sys.stdout", new_callable=io.StringIO))
                patches.enter_context(patch("sys.stderr", new_callable=io.StringIO))
                kwargs = {"remote_attached": True}
                if mode == "bootstrap":
                    kwargs["bootstrap_session"] = source
                if mode in ("launch-rejected", "disconnected"):
                    with self.assertRaises(ValueError if mode == "launch-rejected" else EndTest):
                        cli.run("Test", "origin", **kwargs)
                else:
                    self.assertEqual(cli.run("Test", "origin", **kwargs), 0)
                self.assertEqual(source.read_text(), original)
                self.assertEqual(len(launched), 1)
                command, runtime, text = launched[0]
                self.assertEqual(command[:4], ["kitty", "--single-instance", "--instance-group", "test-instance"])
                self.assertIn("--env=KZW_RPC_SOCKET=" + str(broker.path), text)
                self.assertIn("--var=kzw_workspace=Test", text)
                self.assertFalse(runtime.exists())
                self.assertEqual((directory / "view.json").exists(), mode == "disconnected")
                status_stop.assert_called_once()
                title_stop.set.assert_called_once()
                broker.close.assert_called_once()
                mocks["subprocess.Popen"].assert_not_called()
                with open(directory / "lock", "a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if mode == "bootstrap":
                    self.assertEqual(events[:4], ["bootstrap deferred", "bootstrap ready", "ready", "capture"])
                if mode == "launch-rejected":
                    self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
