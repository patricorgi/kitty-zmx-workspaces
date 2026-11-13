"""Safe demo regressions: no Kitty windows, SSH, zmx or real sessions."""

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = load("demo_runner", REPO / "scripts/demo.py")
remote = load("demo_remote_probe", REPO / "scripts/demo_remote.py")


def layout():
    def pane(name, rect, active=False):
        return {"pane": name, "rect": rect, "active": active}
    return {"tabs": [
        {"active": False, "layout": "splits", "panes": [pane("matrix", [0, 0, 1000, 800], True)]},
        {"active": True, "layout": "splits", "panes": [
            pane("clock", [0, 0, 490, 800]), pane("monitor", [510, 0, 1000, 390]),
            pane("files", [510, 410, 1000, 800], True)]}]}


class DemoLayoutTests(unittest.TestCase):
    def test_area_is_ninety_percent_not_eighty_one(self):
        rect = demo.centered_frame({"x": 0, "y": 25, "width": 2000, "height": 1000}, .9)
        self.assertAlmostEqual(rect["width"] * rect["height"] / 2000000, .9, places=3)
        self.assertLessEqual(abs(rect["x"] * 2 + rect["width"] - 2000), 1)
        self.assertLessEqual(abs((rect["y"] - 25) * 2 + rect["height"] - 1000), 1)

    def test_negative_origin_display(self):
        rect = demo.centered_frame({"x": -2000, "y": -100, "width": 1800, "height": 1200}, .9)
        self.assertLess(rect["x"], -1900)
        self.assertLess(rect["y"], 0)

    def test_full_area(self):
        screen = {"x": -10, "y": 20, "width": 1000, "height": 800}
        self.assertEqual(demo.centered_frame(screen, 1), screen)

    def test_requested_bsp(self):
        demo.check_bsp(layout(), {role: role for role in demo.EXPECTED})

    def test_reject_wrong_layout(self):
        state = layout()
        state["tabs"][1]["layout"] = "tall"
        with self.assertRaises(demo.DemoError):
            demo.check_bsp(state, {role: role for role in demo.EXPECTED})

    def test_reject_reversed_right_panes(self):
        state = layout()
        panes = state["tabs"][1]["panes"]
        panes[1]["rect"], panes[2]["rect"] = panes[2]["rect"], panes[1]["rect"]
        with self.assertRaises(demo.DemoError):
            demo.check_bsp(state, {role: role for role in demo.EXPECTED})

    def test_restore_tolerates_pixel_scale_but_not_proportion_change(self):
        before, after = layout(), layout()
        for tab in after["tabs"]:
            for pane in tab["panes"]:
                pane["rect"] = [coordinate * 2 + 20 for coordinate in pane["rect"]]
        demo.check_restored(before, after)
        after["tabs"][1]["panes"][0]["rect"][2] += 200
        with self.assertRaisesRegex(demo.DemoError, "proportions"):
            demo.check_restored(before, after)

    def test_restore_rejects_tab_order_and_focus_changes(self):
        after = layout()
        after["tabs"].reverse()
        with self.assertRaises(demo.DemoError):
            demo.check_restored(layout(), after)
        after = layout()
        after["tabs"][1]["panes"][2]["active"] = False
        after["tabs"][1]["panes"][0]["active"] = True
        with self.assertRaises(demo.DemoError):
            demo.check_restored(layout(), after)

    def test_program_quoting_and_two_second_clock(self):
        tools = {name: "/home/demo user's/bin/" + name
                 for name in ("cmatrix", "htop", "yazi", "date", "sleep")}
        commands = demo.programs(tools, "/home/demo user's/dotfiles/.config/nvim")
        self.assertIn("sleep' 2", commands["clock"])
        self.assertIn("-u 8", commands["matrix"])
        self.assertIn("-d 20", commands["monitor"])
        import shlex
        self.assertEqual(shlex.split(commands["files"])[2], "/home/demo user's/dotfiles/.config/nvim")

    def test_invalid_arguments_do_not_create_runner_or_resources(self):
        for args in (["--host=-bad"], ["--area", "nan"], ["--area", "0"],
                     ["--pause", "-1"], ["--detach-pause", "inf"], ["--name", "../Demo"]):
            with self.subTest(args=args), patch.object(demo, "Demo") as runner:
                with self.assertRaises((demo.DemoError, ValueError)):
                    demo.main(args)
                runner.assert_not_called()


class DemoProcessProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        boot = self.proc / "sys/kernel/random"
        boot.mkdir(parents=True)
        (boot / "boot_id").write_text("demo-boot\n")

    def process(self, pid, role, command, token="ours", tick="12345"):
        path = self.proc / str(pid)
        path.mkdir()
        (path / "environ").write_bytes(("KZW_DEMO_TOKEN=" + token + "\0KZW_DEMO_ROLE=" + role + "\0").encode())
        (path / "cmdline").write_bytes(command.encode() + b"\0")
        # 19 fields before starttime, counting from state (field 3).
        (path / "stat").write_text(str(pid) + " (odd (process) name) " + "S " + "0 " * 18 + tick + " 0\n")

    def test_exact_token_role_and_executable_only(self):
        self.process(10, "matrix", "/bin/cmatrix")
        self.process(11, "clock", "/bin/bash")
        self.process(12, "clock", "/bin/sleep")  # Clock child, not the loop owner.
        self.process(13, "monitor", "/bin/htop", token="someone-else")
        self.process(14, "files", "/bin/yazi")
        result = remote.processes("ours", demo.EXPECTED, self.proc)
        self.assertEqual(result["boot_id"], "demo-boot")
        self.assertEqual(result["processes"], {
            "matrix": {"pid": 10, "start_tick": "12345"},
            "clock": {"pid": 11, "start_tick": "12345"},
            "files": {"pid": 14, "start_tick": "12345"}})

    def test_duplicate_process_is_error_not_arbitrary_selection(self):
        self.process(10, "files", "/bin/yazi")
        self.process(11, "files", "/bin/yazi")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            remote.processes("ours", demo.EXPECTED, self.proc)

    def test_vanished_process_is_missing_not_recreated(self):
        (self.proc / "10").mkdir()
        self.assertEqual(remote.processes("ours", demo.EXPECTED, self.proc)["processes"], {})

    def test_reused_pid_is_not_accepted(self):
        self.process(10, "files", "/bin/yazi")
        with patch.object(remote, "start_tick", side_effect=["old", "new"]):
            self.assertEqual(remote.processes("ours", demo.EXPECTED, self.proc)["processes"], {})

    def test_empty_fence_rejected(self):
        with self.assertRaises(ValueError):
            remote.processes("", demo.EXPECTED, self.proc)

    def test_animation_owner_is_bash_not_its_sleep_child(self):
        self.process(10, "matrix", "/bin/bash")
        self.process(11, "matrix", "/bin/sleep")
        expected = {**demo.EXPECTED, "matrix": "bash"}
        result = remote.processes("ours", expected, self.proc)
        self.assertEqual(result["processes"], {"matrix": {"pid": 10, "start_tick": "12345"}})


class DemoPrerequisiteTests(unittest.TestCase):
    def cargo_program(self, home, mode=0o700):
        program = home / ".cargo/bin/yazi"
        program.parent.mkdir(parents=True)
        program.write_text("#!/bin/sh\nexit 0\n")
        program.chmod(mode)
        return program

    def test_cargo_yazi_is_found_without_interactive_shell_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            program = self.cargo_program(home)
            with patch.object(remote.Path, "home", return_value=home), patch.object(remote.shutil, "which", return_value=None):
                self.assertEqual(remote.find_program("yazi"), str(program))

    def test_path_yazi_takes_precedence_over_cargo_fallback(self):
        with patch.object(remote.shutil, "which", return_value="/preferred/bin/yazi"), \
                patch.object(remote.Path, "home") as home:
            self.assertEqual(remote.find_program("yazi"), "/preferred/bin/yazi")
        home.assert_not_called()

    def test_nonexecutable_cargo_yazi_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.cargo_program(home, mode=0o600)
            with patch.object(remote.Path, "home", return_value=home), patch.object(remote.shutil, "which", return_value=None):
                self.assertIsNone(remote.find_program("yazi"))

    def test_cargo_directory_named_yazi_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".cargo/bin/yazi").mkdir(parents=True)
            with patch.object(remote.Path, "home", return_value=home), patch.object(remote.shutil, "which", return_value=None):
                self.assertIsNone(remote.find_program("yazi"))

    def test_preflight_checks_and_launches_the_resolved_cargo_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            program = self.cargo_program(home)
            (home / "dotfiles/.config/nvim").mkdir(parents=True)
            with patch.object(remote.sys, "platform", "linux"), patch.object(remote.Path, "home", return_value=home), \
                    patch.object(remote.shutil, "which", side_effect=lambda name: None if name in ("yazi", "cmatrix") else "/bin/" + name), \
                    patch.object(remote.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="version", stderr="")) as run:
                result = remote.preflight()
            self.assertEqual(result["tools"]["yazi"], str(program))
            run.assert_any_call([str(program), "--version"], capture_output=True, text=True, timeout=5)
            import shlex
            command = shlex.split(demo.programs(result["tools"], result["target"])["files"])
            self.assertEqual(command, ["exec", str(program), str(home / "dotfiles/.config/nvim")])

    def test_cmatrix_is_optional_and_its_version_is_not_probed_when_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "dotfiles/.config/nvim").mkdir(parents=True)
            with patch.object(remote.sys, "platform", "linux"), patch.object(remote.Path, "home", return_value=home), \
                    patch.object(remote.shutil, "which", side_effect=lambda name: None if name == "cmatrix" else "/bin/" + name), \
                    patch.object(remote.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="version", stderr="")) as run:
                result = remote.preflight()
        self.assertIsNone(result["tools"]["cmatrix"])
        self.assertNotIn("cmatrix", result["versions"])
        self.assertEqual(run.call_count, 3)

    def test_all_required_missing_tools_are_reported_together(self):
        with patch.object(remote.sys, "platform", "linux"), \
                patch.object(remote, "find_program", side_effect=lambda name: None if name in ("cmatrix", "htop", "yazi") else "/bin/" + name):
            with self.assertRaises(remote.ProbeError) as raised:
                remote.preflight()
        self.assertEqual(raised.exception.code, "missing-programs")
        self.assertEqual(raised.exception.programs, ["htop", "yazi"])

    def test_prerequisite_failure_is_structured_not_a_generic_ssh_failure(self):
        output = io.StringIO()
        with patch.object(remote.sys, "argv", ["probe", '{"mode":"preflight"}']), \
                patch.object(remote, "preflight", side_effect=remote.ProbeError("missing-programs", ["htop"])), \
                patch.object(remote.sys, "stdout", output):
            remote.main()
        result = json.loads(output.getvalue())
        self.assertEqual(demo.probe_error(result["error"]), "missing remote programs: htop")

    def test_unexpected_remote_text_is_not_echoed_to_recording(self):
        for error in ({"code": "private-token"}, {"code": "missing-programs", "programs": ["private-token"]},
                      "private-token"):
            self.assertNotIn("private-token", demo.probe_error(error))

    def test_fallback_animation_runs_without_cmatrix_and_restores_terminal(self):
        tools = {name: "/bin/" + name for name in ("sleep", "date", "htop", "yazi")}
        tools["cmatrix"] = None
        program = demo.programs(tools, "/nvim")["matrix"]
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", program],
                                env={**os.environ, "KZW_DEMO_FRAMES": "2"}, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Frame: 000001", result.stdout)
        self.assertTrue(result.stdout.endswith("\033[0m\033[?25h\033[?1049l"))

    def test_start_preserves_animation_escapes_newlines_and_unicode(self):
        runner = object.__new__(demo.Demo)
        runner.tools = {"env": "/bin/env", "bash": "/bin/bash", "python3": "/usr/bin/python3"}
        runner.token = "token"
        program = "printf '\\033[32m路径\\n'\nprintf 'done'"
        runner.commands = {"matrix": program}
        runner.rc = Mock()
        runner.start({"id": 4}, "matrix")
        command = runner.rc.call_args_list[0].kwargs["input"]
        self.assertNotIn("\n", command)
        self.assertNotIn("\r", command)
        self.assertIn("--stdin", runner.rc.call_args_list[0].args)
        self.assertIn("--bracketed-paste=auto", runner.rc.call_args_list[0].args)
        self.assertEqual(runner.rc.call_args_list[1].kwargs, {"input": "\r"})
        import base64
        import shlex
        bootstrap = shlex.split(command)[-1]
        payload = bootstrap.split("base64.b64decode(", 1)[1].split(")", 1)[0]
        import ast
        self.assertEqual(json.loads(base64.b64decode(ast.literal_eval(payload))), ["/bin/bash", program])

    def test_process_probe_uses_the_selected_startup_program_identity(self):
        runner = object.__new__(demo.Demo)
        runner.token = "token"
        runner.expected = {**demo.EXPECTED, "matrix": "bash"}
        runner.remote = Mock()
        runner.process_state()
        runner.remote.assert_called_once_with("processes", token="token", expected=runner.expected)


class DemoKittenTests(unittest.TestCase):
    def setUp(self):
        handler = SimpleNamespace(result_handler=lambda **kwargs: lambda function: function)
        with patch.dict(sys.modules, {"kittens.tui.handler": handler}):
            self.kitten = load("demo_kitten_test", REPO / "scripts/demo_kitten.py")
        self.window = SimpleNamespace(
            id=4, os_window_id=2, user_vars={"kzw_workspace": "Test", "kzw_host": "origin", "kzw_pane": "pane"},
            current_marker_spec=None, geometry=SimpleNamespace(left=0, top=0, right=100, bottom=100))
        self.window.set_user_var = Mock()
        self.window.set_marker = Mock(side_effect=lambda spec: setattr(self.window, "current_marker_spec", tuple(spec)))

        class Tab(list):
            id = 3
            current_layout = SimpleNamespace(name="splits")

        self.tab = Tab([self.window])
        self.tab.active_window = self.window
        manager = SimpleNamespace(tabs=[self.tab], active_tab=self.tab)
        self.boss = SimpleNamespace(window_id_map={4: self.window}, os_window_map={2: manager})
        self.request = {"name": "Test", "host": "origin", "os_window_id": 2, "windows": [4], "marker": "/private/marker.py"}

    def invoke(self):
        return json.loads(self.kitten.handle_result(["demo_kitten.py", json.dumps(self.request)], None, 4, self.boss))

    def test_explicit_marker_not_toggle_and_inherited_by_future_panes(self):
        state = self.invoke()
        self.window.set_marker.assert_called_once_with(["function", "/private/marker.py"])
        self.window.set_user_var.assert_called_once_with("kzw_marker", json.dumps(["function", "/private/marker.py"]))
        self.assertEqual(state["tabs"][0]["panes"][0]["pane"], "pane")

    def test_foreign_workspace_host_and_window_are_rejected_before_mutation(self):
        for key, value in (("name", "Other"), ("host", "other-host"), ("os_window_id", 99), ("windows", [4, 8])):
            original = copy.deepcopy(self.request)
            self.request[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.invoke()
            self.window.set_marker.assert_not_called()
            self.request = original


class DemoRunnerSafetyTests(unittest.TestCase):
    def mask_runner(self, foreground):
        runner = object.__new__(demo.Demo)
        runner.args = SimpleNamespace(name="Test", host="origin")
        runner.os_id, runner.anchor = 2, 4
        runner.marker = "/private/marker.py"
        runner.mask_color = "#282839"
        runner.title = "KZW Demo test"
        runner.discover = Mock(return_value=[{"id": 4}])
        runner.rc = Mock(side_effect=["{}", "mark1_foreground " + foreground + "\nmark1_background #282839\n", ""])
        return runner

    def test_actual_pane_colors_checked_before_title_or_show(self):
        runner = self.mask_runner("#282839")
        runner.mask(1)
        runner.rc.assert_any_call("get-colors", "--match", "id:4")
        runner.rc.assert_any_call("set-window-title", "--match", "id:4", "KZW Demo test")

    def test_visible_highlight_colors_are_not_accepted_as_redaction(self):
        runner = self.mask_runner("#ffffff")
        with self.assertRaisesRegex(demo.DemoError, "masking colors"):
            runner.mask(1)
        self.assertEqual(runner.rc.call_count, 2)

    def test_shared_launcher_starts_hidden_without_editing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helpers = root / "config/helpers"
            helpers.mkdir(parents=True)
            marker = helpers / "privacy_marker.py"
            marker.write_text("def marker(text):\n    yield 0, len(text) - 1, 1\n")
            local = helpers.parent / "local.conf"
            original = "mark1_foreground #282839\nmark1_background #282839\n"
            local.write_text(original)
            runner = object.__new__(demo.Demo)
            runner.args = SimpleNamespace(marker=marker, host="origin", name="Test", area=.9)
            runner.env = {"PATH": "/bin"}
            runner.kitty = "/tool/kitty"
            runner.run_dir = root / "run"
            runner.run_dir.mkdir()
            runner.note = Mock()
            runner.remote = Mock(return_value={"tools": {name: "/bin/" + name for name in
                                                ("cmatrix", "htop", "yazi", "date", "sleep")}, "target": "/nvim"})
            runner.window = Mock(return_value={"x": 0, "y": 0, "width": 1000, "height": 800})
            with patch.object(demo.sys, "platform", "darwin"), patch.object(demo.shutil, "which", return_value="/tool"), \
                    patch("kzw.kitty_instance.context", return_value={"pid": 123, "address": "unix:source", "group": ""}), \
                    patch("kzw.kitty_instance.probe"), \
                    patch("kzw.remote_client.call", return_value={"exists": False}):
                runner.preflight()
            wrapper = runner.run_dir / "bin/kitty"
            self.assertIn("--start-as=hidden", wrapper.read_text())
            self.assertNotIn("mark1_foreground=", wrapper.read_text())
            self.assertNotIn("shell=", wrapper.read_text())
            self.assertIn('"${1-}" = --single-instance', wrapper.read_text())
            self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)
            self.assertEqual(local.read_text(), original)

    def test_attach_uses_scoped_shared_view_and_creates_before_background_attach(self):
        runner = object.__new__(demo.Demo)
        runner.args = SimpleNamespace(name="Test", host="origin")
        runner.shared = {"pid": 123, "address": "unix:source"}
        runner.kzw = Mock()
        view = {"instance_pid": 123, "address": "unix:source", "os_window_id": 8}
        with patch("kzw.cli.state_dir", return_value=Path("/fake/Test")), patch("kzw.cli.view_state", return_value=view):
            runner.attach(create=True)
        self.assertEqual(runner.kzw.call_args_list, [
            unittest.mock.call("create", "Test", "--host", "origin"),
            unittest.mock.call("attach", "Test", "--host", "origin")])
        self.assertEqual((runner.address, runner.os_id, runner.anchor), ("unix:source", 8, None))

    def test_demo_directions_compensate_for_current_cli_split_axes(self):
        for direction, managed_kind in (("right", "below"), ("below", "right"), ("tab", "tab")):
            runner = object.__new__(demo.Demo)
            runner.args = SimpleNamespace(name="Test", host="origin")
            source = {"id": 4, "user_vars": {"kzw_pane": "source"}}
            added = {"id": 5, "user_vars": {"kzw_pane": "added"}}
            runner.discover = Mock(side_effect=[[source], [source, added]])
            runner.kzw, runner.mask = Mock(), Mock()
            with self.subTest(direction=direction):
                self.assertEqual(runner.new_pane(source, direction, 1), added)
                runner.kzw.assert_called_once_with("_managed-create", "Test", managed_kind, "4", "source",
                                                   "--host", "origin", "--origin", "origin")

    def test_creation_retries_only_an_unchanged_pre_mutation_layout_race(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = object.__new__(demo.Demo)
            runner.args = SimpleNamespace(name="Test", host="origin")
            runner.directory = Path(directory)
            source = {"id": 4, "user_vars": {"kzw_pane": "source"}}
            added = {"id": 5, "user_vars": {"kzw_pane": "added"}}
            runner.discover = Mock(side_effect=[[source], [source], [source, added]])
            error = demo.CommandError(["kzw"], subprocess.CompletedProcess([], 1, "", "kzw: layout changed while taking snapshot\n"))
            runner.kzw, runner.mask = Mock(side_effect=[error, None]), Mock()
            with patch.object(demo.time, "sleep"):
                self.assertEqual(runner.new_pane(source, "right", 1), added)
            self.assertEqual(runner.kzw.call_count, 2)

    def test_pending_creation_blocks_layout_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = object.__new__(demo.Demo)
            runner.args = SimpleNamespace(name="Test", host="origin")
            runner.directory = Path(directory)
            (runner.directory / "pending-create.json").write_text("{}")
            source = {"id": 4, "user_vars": {"kzw_pane": "source"}}
            runner.discover = Mock(return_value=[source])
            error = demo.CommandError(["kzw"], subprocess.CompletedProcess([], 1, "", "kzw: layout changed while taking snapshot"))
            runner.kzw = Mock(side_effect=error)
            with self.assertRaises(demo.CommandError):
                runner.new_pane(source, "right", 1)
            runner.kzw.assert_called_once()

    def test_ssh_or_other_creation_failure_is_not_retried(self):
        runner = object.__new__(demo.Demo)
        runner.args = SimpleNamespace(name="Test", host="origin")
        source = {"id": 4, "user_vars": {"kzw_pane": "source"}}
        runner.discover = Mock(return_value=[source])
        runner.kzw = Mock(side_effect=demo.CommandError(["kzw"], subprocess.CompletedProcess([], 1, "", "SSH failed")))
        with self.assertRaises(demo.CommandError):
            runner.new_pane(source, "right", 1)
        runner.kzw.assert_called_once()

    def test_attach_rejects_a_view_from_a_different_instance(self):
        runner = object.__new__(demo.Demo)
        runner.args = SimpleNamespace(name="Test", host="origin")
        runner.shared = {"pid": 123, "address": "unix:source"}
        runner.kzw = Mock()
        view = {"instance_pid": 999, "address": "unix:other", "os_window_id": 8}
        with patch("kzw.cli.state_dir", return_value=Path("/fake/Test")), patch("kzw.cli.view_state", return_value=view):
            with self.assertRaisesRegex(demo.DemoError, "expected scoped Kitty view"):
                runner.attach()
        self.assertIsNone(runner.address)

    def test_discovery_ignores_other_workspaces_and_unmanaged_source_windows(self):
        runner = object.__new__(demo.Demo)
        runner.args = SimpleNamespace(name="Test", host="origin")
        runner.os_id = 8
        pane = {"id": 7, "user_vars": {"kzw_workspace": "Test", "kzw_host": "origin"}}
        other = {"id": 9, "user_vars": {"kzw_workspace": "Other", "kzw_host": "origin"}}
        runner.rc = Mock(return_value=json.dumps([
            {"id": 1, "tabs": [{"windows": [other]}]},
            {"id": 8, "tabs": [{"windows": [pane]}]},
            {"id": 20, "tabs": [{"windows": [{"id": 22}]}]}]))
        self.assertEqual(runner.discover(1), [pane])
        self.assertEqual(runner.anchor, 7)

    def test_discovery_never_adopts_a_missing_scoped_window(self):
        runner = object.__new__(demo.Demo)
        runner.args = SimpleNamespace(name="Test", host="origin")
        runner.os_id = 8
        runner.rc = Mock(return_value='[{"id":9,"tabs":[]}]')
        with self.assertRaisesRegex(demo.DemoError, "missing or ambiguous"):
            runner.discover(1)

    def test_discovery_rejects_foreign_panes_inside_the_recorded_view(self):
        runner = object.__new__(demo.Demo)
        runner.args = SimpleNamespace(name="Test", host="origin")
        runner.os_id = 8
        runner.rc = Mock(return_value=json.dumps([{"id": 8, "tabs": [{"windows": [
            {"id": 7, "user_vars": {"kzw_workspace": "Other", "kzw_host": "origin"}}]}]}]))
        with self.assertRaisesRegex(demo.DemoError, "foreign window"):
            runner.discover(1)

    def test_detach_waits_for_view_marker_removal(self):
        runner = object.__new__(demo.Demo)
        with tempfile.TemporaryDirectory() as directory:
            runner.directory = Path(directory)
            (runner.directory / "view.json").write_text("{}")
            self.assertFalse(runner.detached())

    def test_startup_failure_retains_supervisor_explanation_without_killing_it(self):
        runner = object.__new__(demo.Demo)
        with tempfile.TemporaryDirectory() as directory:
            runner.directory = Path(directory)
            runner.log_path = runner.directory / "diagnostics.log"
            (runner.directory / "supervisor.log").write_text("snapshot deferred: managed panes span multiple OS windows\n")
            runner.run = Mock(side_effect=demo.DemoError("startup failed"))
            with self.assertRaises(demo.DemoError):
                runner.kzw("attach", "Test", "--host", "origin")
            self.assertIn("managed panes span multiple OS windows", runner.log_path.read_text())


class DemoWorkflowTests(unittest.TestCase):
    def runner(self):
        # Bypass __init__: all external interfaces below are mocked, no run directory.
        runner = object.__new__(demo.Demo)
        runner.args = SimpleNamespace(check=False, pause=0, detach_pause=0, name="Test", host="origin")
        runner.startup_label = "cmatrix"
        runner.note = Mock()
        runner.preflight = Mock()
        runner.reset_existing = Mock()
        runner.attach = Mock()
        runner.hide = Mock()
        runner.kzw = Mock()
        runner.rc = Mock()
        runner.start = Mock()
        runner.detached = Mock(return_value=True)
        runner.ended = Mock(return_value=True)
        runner.address = "unix:fake"
        windows = [{"id": n, "user_vars": {"kzw_pane": role}} for n, role in enumerate(demo.EXPECTED, 1)]
        runner.discover = Mock(side_effect=lambda count: windows[:count])
        runner.new_pane = Mock(side_effect=windows[1:])
        runner.mask = Mock(side_effect=lambda count: layout())
        runner.show = Mock(side_effect=lambda count: layout())
        baseline = {"boot_id": "boot", "processes": {role: {"pid": n, "start_tick": "1"}
                                                  for n, role in enumerate(demo.EXPECTED)}}
        runner.ready = Mock(return_value=baseline)
        runner.process_state = Mock(return_value=baseline)
        runner.run_dir = Path("/not-a-real-demo-run")
        return runner

    def test_check_stops_before_creation(self):
        runner = self.runner()
        runner.args.check = True
        runner.execute()
        runner.attach.assert_not_called()
        runner.reset_existing.assert_not_called()
        runner.kzw.assert_not_called()
        runner.ended.assert_not_called()

    def test_final_scene_is_held_before_detach_and_confirmed_termination(self):
        runner = self.runner()
        order = Mock()
        order.attach_mock(runner.rc, "rc")
        order.attach_mock(runner.kzw, "kzw")
        order.attach_mock(runner.detached, "detached")
        order.attach_mock(runner.ended, "ended")
        with patch.object(Path, "write_text"), patch.object(demo.time, "sleep") as sleep:
            order.attach_mock(sleep, "sleep")
            runner.execute()
        self.assertEqual(order.mock_calls[-6:], [
            unittest.mock.call.rc("focus-window", "--match", "id:4"),
            unittest.mock.call.sleep(10),
            unittest.mock.call.kzw("detach", "Test", "--host", "origin"),
            unittest.mock.call.detached(),
            unittest.mock.call.kzw("kill", "Test", "--host", "origin", input="y\n"),
            unittest.mock.call.ended()])
        self.assertIsNone(runner.address)
        self.assertIn("workspace ended", runner.note.call_args.args[0])

    def test_final_detach_failure_or_timeout_never_kills(self):
        for failure in (demo.DemoError("detach failed"), None):
            runner = self.runner()
            runner.kzw.side_effect = failure
            runner.wait = Mock(side_effect=demo.DemoError("detach timeout"))
            with self.subTest(failure=failure), patch.object(demo.time, "sleep"), \
                    self.assertRaises(demo.DemoError):
                runner.finish()
            runner.kzw.assert_called_once_with("detach", "Test", "--host", "origin")
            runner.ended.assert_not_called()

    def test_kill_failure_does_not_report_success_or_retry(self):
        runner = self.runner()
        runner.kzw.side_effect = [None, demo.DemoError("cleanup pending")]
        with patch.object(demo.time, "sleep"), self.assertRaisesRegex(demo.DemoError, "cleanup pending"):
            runner.finish()
        self.assertEqual(runner.kzw.call_count, 2)
        runner.ended.assert_not_called()
        self.assertFalse(any("workspace ended" in call.args[0] for call in runner.note.call_args_list))

    def test_termination_timeout_does_not_report_success(self):
        runner = self.runner()
        runner.wait = Mock(side_effect=[True, demo.DemoError("termination timeout")])
        with patch.object(demo.time, "sleep"), self.assertRaisesRegex(demo.DemoError, "termination timeout"):
            runner.finish()
        self.assertFalse(any("workspace ended" in call.args[0] for call in runner.note.call_args_list))

    def test_final_hold_interruption_never_starts_cleanup(self):
        runner = self.runner()
        with patch.object(demo.time, "sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            runner.finish()
        runner.kzw.assert_not_called()

    def test_ended_requires_explicit_origin_confirmation(self):
        runner = self.runner()
        for state, expected in (({"exists": True}, False), ({"exists": False}, True)):
            with self.subTest(state=state), patch("kzw.remote_client.call", return_value=state) as call:
                self.assertEqual(demo.Demo.ended(runner), expected)
                call.assert_called_once_with("origin", {"method": "workspace.sync", "workspace": "Test"})
        for state in ({}, {"exists": None}, {"exists": 0}):
            with self.subTest(state=state), patch("kzw.remote_client.call", return_value=state), \
                    self.assertRaisesRegex(demo.DemoError, "unconfirmed"):
                demo.Demo.ended(runner)
        with patch("kzw.remote_client.call", side_effect=ValueError("SSH failed")), self.assertRaises(ValueError):
            demo.Demo.ended(runner)

    def test_kzw_passes_confirmation_to_normal_cli(self):
        runner = self.runner()
        runner.run = Mock(return_value="terminated")
        self.assertEqual(demo.Demo.kzw(runner, "kill", "Test", "--host", "origin", input="y\n"), "terminated")
        runner.run.assert_called_once_with(
            [sys.executable, "-m", "kzw.cli", "kill", "Test", "--host", "origin"], timeout=180, input="y\n")

    def test_failed_reset_blocks_creation_and_program_startup(self):
        runner = self.runner()
        runner.reset_existing.side_effect = demo.DemoError("cleanup is pending")
        with self.assertRaisesRegex(demo.DemoError, "cleanup is pending"):
            runner.execute()
        runner.attach.assert_not_called()
        runner.start.assert_not_called()

    def test_reset_finishes_before_any_new_attachment(self):
        runner = self.runner()
        order = Mock()
        order.attach_mock(runner.reset_existing, "reset")
        order.attach_mock(runner.attach, "attach")
        with patch.object(Path, "write_text"), patch.object(demo.time, "sleep"):
            runner.execute()
        self.assertEqual(order.mock_calls[:2], [unittest.mock.call.reset(), unittest.mock.call.attach(create=True)])

    def test_full_sequence_never_replays_programs_on_reattach(self):
        runner = self.runner()
        with patch.object(Path, "write_text"), patch.object(demo.time, "sleep"):
            runner.execute()
        self.assertEqual(runner.attach.call_args_list, [unittest.mock.call(create=True), unittest.mock.call()])
        runner.reset_existing.assert_called_once_with()
        self.assertEqual([call.args[1] for call in runner.start.call_args_list], list(demo.EXPECTED))
        self.assertEqual([call.args[1] for call in runner.new_pane.call_args_list], ["tab", "right", "below"])
        runner.rc.assert_any_call("goto-layout", "--match", "window_id:2", "splits")
        runner.kzw.assert_any_call("detach", "Test", "--host", "origin")
        self.assertFalse(any(call.args[0] == "save" for call in runner.kzw.call_args_list))
        self.assertEqual(runner.process_state.call_count, 2)

    def test_changed_detached_process_stops_before_reattach(self):
        runner = self.runner()
        runner.process_state.return_value = {"boot_id": "different-boot", "processes": {}}
        with patch.object(demo.time, "sleep"), self.assertRaisesRegex(demo.DemoError, "while detached"):
            runner.execute()
        runner.attach.assert_called_once_with(create=True)
        self.assertEqual(runner.start.call_count, 4)
        self.assertFalse(any(call.args[0] == "kill" for call in runner.kzw.call_args_list))

    def test_changed_reattached_process_is_not_restarted(self):
        runner = self.runner()
        baseline = runner.ready.return_value
        runner.process_state.side_effect = [baseline, {"boot_id": "boot", "processes": {}}]
        with patch.object(demo.time, "sleep"), self.assertRaisesRegex(demo.DemoError, "after reattachment"):
            runner.execute()
        self.assertEqual(runner.start.call_count, 4)
        self.assertFalse(any(call.args[0] == "kill" for call in runner.kzw.call_args_list))


if __name__ == "__main__":
    unittest.main()
