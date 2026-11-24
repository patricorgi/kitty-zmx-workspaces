"""Bindings in a shared Kitty route by the selected pane, not server env."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

A, B = ("kzw-" + letter * 32 for letter in "ab")


def kitten(name):
    handler = types.ModuleType("kittens.tui.handler")
    handler.result_handler = lambda **kwargs: lambda function: function
    fast = types.ModuleType("kitty.fast_data_types")
    fast.add_timer, fast.remove_timer = Mock(return_value=17), Mock()
    notifications = types.ModuleType("kitty.notifications")
    notifications.OnlyWhen = types.SimpleNamespace(always="always")
    with patch.dict(sys.modules, {"kittens": types.ModuleType("kittens"), "kittens.tui": types.ModuleType("kittens.tui"),
                                  "kittens.tui.handler": handler, "kitty.fast_data_types": fast,
                                  "kitty.notifications": notifications}):
        spec = importlib.util.spec_from_file_location("routing_" + name, Path(__file__).parents[1] / "kzw" / (name + "_kitten.py"))
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
    return result


class Tab(list):
    id = 4


def view(origin):
    windows = [types.SimpleNamespace(id=i, tab_id=4, os_window_id=42,
                                    user_vars={"kzw_workspace": "Test", "kzw_origin": origin,
                                               "kzw_pane": pane, "kzw_host": origin},
                                    screen=types.SimpleNamespace(last_reported_cwd=b"file://host/a%20b"))
               for i, pane in ((7, A), (8, B))]
    if origin is None:
        for window in windows:
            window.user_vars.pop("kzw_origin")
            window.user_vars.pop("kzw_host")
    boss = Mock(window_id_map={w.id: w for w in windows})
    boss.os_window_map = {42: types.SimpleNamespace(tabs=[windows])}
    boss.tab_for_id.return_value = Tab(windows)
    return windows, boss


class KittenRoutingTests(unittest.TestCase):
    def setUp(self):
        context = patch.dict(os.environ, {"KZW_WORKSPACE": "Wrong", "KZW_WORKSPACE_ORIGIN": "wrong-origin",
                                          "KZW_HOST": "wrong-host"}, clear=True)
        context.start()
        self.addCleanup(context.stop)

    def test_create_routes_every_managed_action_using_window_identity(self):
        module = kitten("create")
        for kind in ("right", "below", "split", "tab"):
            for origin in ("origin", "", None):
                with self.subTest(kind=kind, origin=origin):
                    _, boss = view(origin)
                    origin = origin or ""
                    with patch.object(module.shutil, "which", return_value="/on-path/kzw"), \
                            patch.object(module.subprocess, "Popen") as popen:
                        module.handle_result(["kitten", kind], None, 7, boss)
                    command = popen.call_args.args[0]
                    self.assertEqual(command[:8], ["/on-path/kzw", "_managed-create", "Test", kind, "7", A, "--host", origin])
                    self.assertIn("file://host/a%20b", command)
                    if origin:
                        self.assertEqual(command[-2:], ["--origin", origin])
                    else:
                        self.assertNotIn("--origin", command)
                    boss.show_error.assert_not_called()

    def test_create_reuses_only_the_selected_panes_broker_and_transport(self):
        module = kitten("create")
        windows, boss = view("origin")
        windows[0].child = types.SimpleNamespace(environ={
            "KZW_RPC_SOCKET": "/selected", "KZW_SSH_KITTEN": "1", "UNRELATED": "not copied",
        })
        with patch.dict(os.environ, {"KZW_RPC_SOCKET": "/wrong", "KZW_SSH_KITTEN": "wrong"}), \
                patch.object(module.subprocess, "Popen") as popen:
            module.handle_result(["kitten", "tab"], None, 7, boss)
            env = popen.call_args.kwargs["env"]
            self.assertEqual((env["KZW_WORKSPACE"], env["KZW_WORKSPACE_ORIGIN"],
                              env["KZW_RPC_SOCKET"], env["KZW_SSH_KITTEN"]),
                             ("Test", "origin", "/selected", "1"))
            self.assertNotIn("UNRELATED", env)
            module.handle_result(["kitten", "tab"], None, 8, boss)
            self.assertNotIn("KZW_RPC_SOCKET", popen.call_args.kwargs["env"])
            self.assertNotIn("KZW_SSH_KITTEN", popen.call_args.kwargs["env"])

    def test_confirmed_close_freezes_selection_and_routes_to_the_selected_workspace(self):
        module = kitten("close")
        for kind in ("pane", "tab"):
            for origin in ("origin", "", None):
                with self.subTest(kind=kind, origin=origin):
                    windows, boss = view(origin)
                    origin = origin or ""
                    with patch.object(module.shutil, "which", return_value="/on-path/kzw"), \
                            patch.object(module.subprocess, "Popen") as popen:
                        module.handle_result(["kitten", kind], None, 7, boss)
                        confirm = boss.confirm.call_args
                        self.assertFalse(confirm.kwargs["confirm_on_accept"])
                        self.assertFalse(confirm.kwargs["confirm_on_cancel"])
                        callback = confirm.args[1]
                        callback(False)
                        popen.assert_not_called()
                        windows[0].user_vars["kzw_pane"] = "kzw-" + "f" * 32
                        callback(True)
                    command = popen.call_args.args[0]
                    self.assertEqual(command[:5], ["/on-path/kzw", "_confirmed-close", "Test", kind, "7" if kind == "pane" else "4"])
                    expected = {"7": [A, origin]}
                    if kind == "tab":
                        expected["8"] = [B, origin]
                    self.assertEqual(json.loads(command[5]), expected)
                    self.assertEqual(command[6:], ["--host", origin] if origin else [])

    def test_confirmed_close_reuses_only_the_selected_panes_broker(self):
        module = kitten("close")
        windows, boss = view("origin")
        windows[0].child = types.SimpleNamespace(environ={"KZW_RPC_SOCKET": "/selected"})
        with patch.dict(os.environ, {"KZW_RPC_SOCKET": "/wrong"}), \
                patch.object(module.subprocess, "Popen") as popen:
            module.handle_result(["kitten", "pane"], None, 7, boss)
            boss.confirm.call_args.args[1](True)
            env = popen.call_args.kwargs["env"]
            self.assertEqual((env["KZW_WORKSPACE"], env["KZW_WORKSPACE_ORIGIN"], env["KZW_RPC_SOCKET"]),
                             ("Test", "origin", "/selected"))
            module.handle_result(["kitten", "pane"], None, 8, boss)
            boss.confirm.call_args.args[1](True)
            self.assertNotIn("KZW_RPC_SOCKET", popen.call_args.kwargs["env"])

    def test_create_and_close_find_installed_launcher_without_shell_path(self):
        for name, kind, operation in (("create", "split", "_managed-create"),
                                      ("close", "pane", "_confirmed-close")):
            with self.subTest(name=name):
                module = kitten(name)
                _, boss = view("origin")
                with patch.dict(os.environ, {"HOME": "/example-home", "PATH": "/usr/bin:/bin"}), \
                        patch.object(module.shutil, "which", return_value=None), \
                        patch.object(module.subprocess, "Popen") as popen:
                    module.handle_result(["kitten", kind], None, 7, boss)
                    if name == "close":
                        boss.confirm.call_args.args[1](True)
                self.assertEqual(popen.call_args.args[0][:2], ["/example-home/.local/bin/kzw", operation])

    def test_detach_routes_by_view_context_and_launch_failures_are_reported(self):
        for name, kind in (("create", "tab"), ("close", "pane"), ("detach", None)):
            for origin in ("origin", "", None):
                with self.subTest(action=name, origin=origin):
                    module = kitten(name)
                    _, boss = view(origin)
                    origin = origin or ""
                    args = ["kitten", kind] if kind else ["kitten"]
                    with patch.object(module.subprocess, "Popen", side_effect=OSError("launcher unavailable")) as popen:
                        module.handle_result(args, None, 7, boss)
                        if name == "close":
                            boss.confirm.call_args.args[1](True)
                    popen.assert_called_once()
                    if name == "detach":
                        command = popen.call_args.args[0]
                        self.assertEqual(Path(command[0]).name, "kzw")
                        self.assertEqual(command[1:], ["detach", "Test"] + (["--host", origin] if origin else []))
                    self.assertIn("launcher unavailable", boss.show_error.call_args.args[1])
