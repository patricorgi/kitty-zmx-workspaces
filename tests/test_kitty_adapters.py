"""Exercise in-Kitty adapters with API doubles, including stale window races."""

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


def load_adapter(name, path=None, native_loader=False):
    handler = types.ModuleType("kittens.tui.handler")
    handler.result_handler = lambda **kwargs: lambda function: function
    session = types.ModuleType("kitty.session")
    session.default_save_as_session_opts = Mock(return_value=object())
    native = types.ModuleType("kitty.fast_data_types")
    native.os_window_font_size = Mock(return_value=14.5)
    native.cocoa_minimize_os_window = Mock()
    native.focus_os_window = Mock()
    with patch.dict(sys.modules, {"kittens": types.ModuleType("kittens"), "kittens.tui": types.ModuleType("kittens.tui"),
                                  "kittens.tui.handler": handler, "kitty": types.ModuleType("kitty"),
                                  "kitty.session": session, "kitty.fast_data_types": native}):
        path = path or Path(__file__).parents[1] / "kzw" / (name + "_kitten.py")
        if native_loader:
            module = types.ModuleType("kitten")
            exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
            return module
        spec = importlib.util.spec_from_file_location("adapter_" + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class KittyAdapterTests(unittest.TestCase):
    def test_snapshot_kitten_loads_without_a_dunder_file_as_in_real_kitty(self):
        module = load_adapter("scoped_session", native_loader=True)
        self.assertTrue(callable(module.handle_result))
        self.assertFalse(hasattr(module, "__file__"))

    def test_exit_close_validates_all_wrappers_before_closing_any(self):
        module = load_adapter("exit")
        expected = {"id": 7, "os_window_id": 42, "pid": 99, "pane": "kzw-" + "a" * 32, "ended": "1"}
        for field, bad in (("id", 8), ("os_window_id", 43), ("pid", 100), ("pane", "other"), ("ended", "0"), (None, None)):
            with self.subTest(field=field):
                actual = {**expected, **({field: bad} if field else {})}
                window = types.SimpleNamespace(id=actual["id"], os_window_id=actual["os_window_id"],
                                               child=types.SimpleNamespace(pid=actual["pid"]),
                                               user_vars={"kzw_pane": actual["pane"], "kzw_attach_ended": actual["ended"]})
                boss = Mock(window_id_map={7: window, 88: Mock()})
                if field:
                    with self.assertRaises(ValueError):
                        module.handle_result(["kitten", json.dumps({"7": expected})], None, 7, boss)
                    boss.mark_window_for_close.assert_not_called()
                else:
                    self.assertEqual(json.loads(module.handle_result(["kitten", json.dumps({"7": expected})], None, 7, boss)),
                                     {"closed": ["7"]})
                    boss.mark_window_for_close.assert_called_once_with(window)

    def test_snapshot_serializes_only_verified_os_window_and_rejects_stale_targets(self):
        module = load_adapter("scoped_session")
        for mode in ("valid", "missing pane", "moved pane", "missing window", "bad args"):
            with self.subTest(mode=mode):
                selected = Mock(tabs=[])
                selected.serialize_state_as_session.return_value = iter(["new_tab Test", "layout splits"])
                unrelated = Mock()
                window = types.SimpleNamespace(os_window_id=99 if mode == "moved pane" else 42)
                boss = Mock(window_id_map={} if mode == "missing pane" else {7: window},
                            os_window_map={99: unrelated} if mode == "missing window" else {42: selected, 99: unrelated})
                args = ["kitten"] if mode == "bad args" else ["kitten", "42"]
                if mode == "valid":
                    result = json.loads(module.handle_result(args, None, 7, boss))
                    self.assertEqual(result, {"os_window_id": 42, "session": "new_tab Test\nlayout splits"})
                    selected.serialize_state_as_session.assert_called_once_with(
                        "", None, module.default_save_as_session_opts.return_value, is_first=True)
                else:
                    with self.assertRaises(ValueError):
                        module.handle_result(args, None, 7, boss)
                    selected.serialize_state_as_session.assert_not_called()
                unrelated.serialize_state_as_session.assert_not_called()

    def test_appearance_reads_exact_source_font_colors_and_marker(self):
        module = load_adapter("appearance")
        window = types.SimpleNamespace(id=7, os_window_id=42, current_marker_spec=["function", "marker.py"])
        boss = Mock(window_id_map={7: window, 8: types.SimpleNamespace(id=8, os_window_id=99)})
        boss.call_remote_control.return_value = "foreground #abcdef"
        result = json.loads(module.handle_result(["kitten"], None, 7, boss))
        self.assertEqual(result, {"font_size": 14.5, "colors": "foreground #abcdef", "marker": window.current_marker_spec})
        module.os_window_font_size.assert_called_once_with(42)
        boss.call_remote_control.assert_called_once_with(window, ("get-colors", "--match", "id:7"))
        boss.call_remote_control.reset_mock()
        with self.assertRaises(ValueError):
            module.handle_result(["kitten"], None, 1234, boss)
        boss.call_remote_control.assert_not_called()

    def test_atomic_snapshot_pairs_layout_with_only_its_workspace_tree(self):
        module = load_adapter("scoped_session")
        selected, unrelated = Mock(tabs=[]), Mock()
        selected.serialize_state_as_session.return_value = iter(["new_tab Test", "layout splits"])
        window = types.SimpleNamespace(os_window_id=42)
        trees = [{"id": i, "tabs": [{"id": i}]} for i in (42, 99)]
        boss = Mock(window_id_map={7: window}, os_window_map={42: selected, 99: unrelated})
        boss.list_os_windows.side_effect = lambda tab_filter: (
            ow for ow in trees if tab_filter(types.SimpleNamespace(os_window_id=ow["id"])))
        result = json.loads(module.handle_result(["kitten", "42", "--with-tree"], None, 7, boss))
        self.assertEqual(result, {"os_window_id": 42, "session": "new_tab Test\nlayout splits", "tree": trees[:1]})
        unrelated.serialize_state_as_session.assert_not_called()
        with self.assertRaises(ValueError):
            module.handle_result(["kitten", "42", "--unknown"], None, 7, boss)

    def test_minimize_restore_use_source_os_window_and_invalid_requests_do_nothing(self):
        module = load_adapter("supervisor_window")
        boss = Mock(window_id_map={7: types.SimpleNamespace(os_window_id=42)})
        for args, target in ((["kitten"], 7), (["kitten", "close"], 7), (["kitten", "minimize"], 8)):
            with self.subTest(args=args, target=target), self.assertRaises(ValueError):
                module.handle_result(args, None, target, boss)
        module.cocoa_minimize_os_window.assert_not_called()
        module.focus_os_window.assert_not_called()
        self.assertEqual(module.handle_result(["kitten", "minimize"], None, 7, boss), "ok")
        module.cocoa_minimize_os_window.assert_called_once_with(42)
        module.focus_os_window.assert_not_called()
        self.assertEqual(module.handle_result(["kitten", "restore"], None, 7, boss), "ok")
        module.focus_os_window.assert_called_once_with(42, True)
