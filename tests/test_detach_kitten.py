import importlib.util
import io
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_kitten():
    handler = types.ModuleType("kittens.tui.handler")
    handler.result_handler = lambda **kwargs: lambda function: function
    fast = types.ModuleType("kitty.fast_data_types")
    fast.add_timer, fast.remove_timer = Mock(return_value=17), Mock()
    with patch.dict(sys.modules, {"kittens": types.ModuleType("kittens"),
                                 "kittens.tui": types.ModuleType("kittens.tui"),
                                 "kittens.tui.handler": handler, "kitty.fast_data_types": fast}):
        spec = importlib.util.spec_from_file_location("test_detach_kitten_impl", Path(__file__).parents[1] / "kzw/detach_kitten.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, fast


class DetachKittenTests(unittest.TestCase):
    def setUp(self):
        self.kitten, self.fast = load_kitten()
        self.window = types.SimpleNamespace(id=7, os_window_id=42, user_vars={
            "kzw_workspace": "Test", "kzw_origin": "origin", "kzw_pane": "kzw-" + "a" * 32},
            child=types.SimpleNamespace(environ={"KZW_RPC_SOCKET": "/broker"}))
        self.manager = types.SimpleNamespace(tabs=[[self.window]], mark_tab_bar_dirty=Mock())
        self.other = types.SimpleNamespace(tabs=[])
        self.boss = Mock(window_id_map={7: self.window}, os_window_map={42: self.manager, 43: self.other})
        self.process = Mock()
        self.process.poll.return_value = None
        self.output = io.BytesIO()

    def start(self):
        with patch.object(self.kitten.subprocess, "Popen", return_value=self.process) as spawn, \
             patch.object(self.kitten.tempfile, "TemporaryFile", return_value=self.output), \
             patch.object(self.kitten.shutil, "which", return_value="/bin/kzw"):
            self.kitten.handle_result([], None, 7, self.boss)
        return spawn, self.fast.add_timer.call_args.args[0]

    def test_workspace_origin_with_scoped_broker(self):
        spawn, callback = self.start()
        self.assertEqual(spawn.call_args.args[0], ["/bin/kzw", "detach", "Test", "--host", "origin"])
        self.assertEqual(spawn.call_args.kwargs["env"]["KZW_RPC_SOCKET"], "/broker")
        self.boss.notification_manager.assert_not_called()
        self.boss.notification_manager.notify_with_command.assert_not_called()
        self.assertIs(self.manager._kzw_detach_in_progress["process"], self.process)
        self.assertEqual((self.manager._kzw_detach_in_progress["workspace"],
                          self.manager._kzw_detach_in_progress["origin"]), ("Test", "origin"))
        self.manager.mark_tab_bar_dirty.assert_called_once()
        self.assertFalse(hasattr(self.other, "_kzw_detach_in_progress"))
        self.assertEqual(self.fast.add_timer.call_args.args[1:], (0.25, True))
        self.boss.mark_os_window_for_close.assert_not_called()

    def test_repeat_is_coalesced_without_notification_or_another_process(self):
        self.start()
        with patch.object(self.kitten.subprocess, "Popen") as spawn:
            self.kitten.handle_result([], None, 7, self.boss)
        spawn.assert_not_called()
        self.boss.notification_manager.notify_with_command.assert_not_called()

    def test_running_process_retains_status_without_notification(self):
        _, callback = self.start()
        callback(17)
        callback(17)
        self.boss.notification_manager.notify_with_command.assert_not_called()
        self.assertIs(self.manager._kzw_detach_in_progress["process"], self.process)
        self.process.terminate.assert_not_called()
        self.boss.mark_os_window_for_close.assert_not_called()

    def test_failed_process_shows_output_and_releases_guard(self):
        _, callback = self.start()
        self.output.write(b"kzw: pending operation retained\n")
        self.process.poll.return_value = 1
        callback(17)
        self.assertIsNone(self.manager._kzw_detach_in_progress)
        self.assertEqual(self.manager.mark_tab_bar_dirty.call_count, 2)
        self.boss.show_error.assert_called_once_with("Workspace detach failed", "kzw: pending operation retained")
        self.assertTrue(self.output.closed)
        self.fast.remove_timer.assert_called_once_with(17)

    def test_success_clears_guard_without_false_error(self):
        _, callback = self.start()
        self.process.poll.return_value = 0
        callback(17)
        self.boss.show_error.assert_not_called()
        self.assertIsNone(self.manager._kzw_detach_in_progress)
        self.assertEqual(self.manager.mark_tab_bar_dirty.call_count, 2)
        self.assertTrue(self.output.closed)

    def test_local_companion_uses_same_workspace_detach(self):
        self.window.user_vars = {"kzw_local": "kzw-" + "b" * 32, "kzw_local_workspace": "Test",
                                 "kzw_local_origin": "origin"}
        spawn, _ = self.start()
        self.assertEqual(spawn.call_args.args[0], ["/bin/kzw", "detach", "Test", "--host", "origin"])
        self.assertNotIn("KZW_RPC_SOCKET", spawn.call_args.kwargs["env"])
        self.assertEqual((self.manager._kzw_detach_in_progress["workspace"],
                          self.manager._kzw_detach_in_progress["origin"]), ("Test", "origin"))

    def test_renderer_only_refreshes_compatible_custom_draw_once(self):
        module = types.ModuleType("kitty.tab_bar")
        old = lambda: None
        old._equal_margins = True
        new = lambda: None
        new._equal_margins = new._kzw_detaching = True
        module.load_custom_draw_tab = Mock(return_value=new)
        module.load_custom_draw_tab_module = Mock()
        self.manager.tab_bar = types.SimpleNamespace(draw_func=old)
        with patch.dict(sys.modules, {"kitty.tab_bar": module}):
            self.kitten.refresh_renderer(self.manager)
            self.kitten.refresh_renderer(self.manager)
        self.assertIs(self.manager.tab_bar.draw_func, new)
        module.load_custom_draw_tab.assert_called_once_with()

    def test_stale_callback_does_not_clear_newer_operation_or_redraw(self):
        _, callback = self.start()
        replacement = {"workspace": "Other", "origin": "elsewhere"}
        self.manager._kzw_detach_in_progress = replacement
        callback(17)
        self.assertIs(self.manager._kzw_detach_in_progress, replacement)
        self.manager.mark_tab_bar_dirty.assert_called_once()
        self.assertTrue(self.output.closed)

    def test_missing_identity_and_spawn_failure_do_not_close_view(self):
        self.window.user_vars = {}
        with patch.dict(os.environ, {}, clear=True), patch.object(self.kitten.subprocess, "Popen") as spawn:
            self.kitten.handle_result([], None, 7, self.boss)
        spawn.assert_not_called()
        self.boss.show_error.assert_called_once()
        self.window.user_vars = {"kzw_workspace": "Test", "kzw_origin": "origin"}
        self.boss.reset_mock()
        with patch.object(self.kitten.subprocess, "Popen", side_effect=OSError("not found")), \
             patch.object(self.kitten.tempfile, "TemporaryFile", return_value=self.output):
            self.kitten.handle_result([], None, 7, self.boss)
        self.boss.show_error.assert_called_once()
        self.assertTrue(self.output.closed)
        self.assertFalse(hasattr(self.manager, "_kzw_detach_in_progress"))
        self.boss.mark_os_window_for_close.assert_not_called()
