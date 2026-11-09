import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_kitten():
    handler = types.ModuleType("kittens.tui.handler")
    handler.result_handler = lambda **kwargs: lambda f: f
    with patch.dict(sys.modules, {"kittens": types.ModuleType("kittens"),
                                 "kittens.tui": types.ModuleType("kittens.tui"),
                                 "kittens.tui.handler": handler}):
        spec = importlib.util.spec_from_file_location("test_close_kitten_impl", Path(__file__).parents[1] / "kzw/close_kitten.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class CloseKittenTests(unittest.TestCase):
    def test_cancel_never_starts_close_and_default_is_cancel(self):
        kitten = load_kitten()
        window = types.SimpleNamespace(id=7, tab_id=4, user_vars={"kzw_pane": "kzw-" + "a" * 32})
        boss = Mock(window_id_map={7: window})
        boss.tab_for_id.return_value = [window]
        with patch.dict("os.environ", {"KZW_WORKSPACE": "Test"}), patch.object(kitten.subprocess, "Popen") as run:
            kitten.handle_result(["kitten", "pane"], None, 7, boss)
            self.assertFalse(boss.confirm.call_args.kwargs["confirm_on_accept"])
            boss.confirm.call_args.args[1](False)
            run.assert_not_called()
            boss.confirm.call_args.args[1](True)
            args = run.call_args.args[0]
            self.assertEqual(args[1:5], ["_confirmed-close", "Test", "pane", "7"])
            self.assertEqual(json.loads(args[5])["7"][0], window.user_vars["kzw_pane"])

    def test_unmanaged_tab_never_prompts(self):
        kitten = load_kitten()
        window = types.SimpleNamespace(id=7, tab_id=4, user_vars={})
        boss = Mock(window_id_map={7: window})
        boss.tab_for_id.return_value = [window]
        kitten.handle_result(["kitten", "tab"], None, 7, boss)
        boss.confirm.assert_not_called()
        boss.show_error.assert_called_once()
