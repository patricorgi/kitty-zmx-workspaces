import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_kitten():
    handler = types.ModuleType("kittens.tui.handler")
    handler.result_handler = lambda **kwargs: lambda function: function
    with patch.dict(sys.modules, {"kittens": types.ModuleType("kittens"),
                                 "kittens.tui": types.ModuleType("kittens.tui"),
                                 "kittens.tui.handler": handler}):
        spec = importlib.util.spec_from_file_location("test_detach_kitten_impl", Path(__file__).parents[1] / "kzw/detach_kitten.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class DetachKittenTests(unittest.TestCase):
    def test_detach_uses_workspace_origin(self):
        kitten = load_kitten()
        boss = Mock(window_id_map={7: object()})
        with patch.dict("os.environ", {"KZW_WORKSPACE": "Test", "KZW_WORKSPACE_ORIGIN": "origin"}, clear=True), \
             patch.object(kitten.subprocess, "Popen") as spawn:
            kitten.handle_result([], None, 7, boss)
        self.assertEqual(spawn.call_args.args[0], ["kzw", "detach", "Test", "--host", "origin"])

    def test_missing_identity_never_detaches(self):
        kitten = load_kitten()
        boss = Mock(window_id_map={7: object()})
        with patch.dict("os.environ", {}, clear=True), patch.object(kitten.subprocess, "Popen") as spawn:
            kitten.handle_result([], None, 7, boss)
        spawn.assert_not_called()
        boss.show_error.assert_called_once()
