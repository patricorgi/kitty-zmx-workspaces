import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


def load_kitten():
    handler = types.ModuleType("kittens.tui.handler")
    handler.result_handler = lambda **kw: lambda fn: fn
    fast = types.ModuleType("kitty.fast_data_types")
    fast.add_timer = Mock(return_value=11)
    fast.remove_timer = Mock()
    with patch.dict(sys.modules, {"kittens.tui.handler": handler, "kitty.fast_data_types": fast}):
        spec = importlib.util.spec_from_file_location("metrics_kitten_test", Path(__file__).parents[1] / "kzw/metrics_kitten.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, fast


class Manager:
    def __init__(self, workspace="Test", origin="remote"):
        self.tabs = [[types.SimpleNamespace(user_vars={"kzw_pane": "kzw-" + "a" * 32,
                                                     "kzw_workspace": workspace, "kzw_origin": origin})]]
        self.mark_tab_bar_dirty = Mock()


class MetricsKittenTests(unittest.TestCase):
    def setUp(self):
        self.module, self.fast = load_kitten()
        self.manager, self.other = Manager(), Manager("Other")
        self.boss = types.SimpleNamespace(os_window_map={2: self.manager, 3: self.other})

    def update(self, sample=None, origin="remote", workspace="Test"):
        data = {"workspace": workspace, "origin": origin, "metrics": sample}
        return self.module.handle_result(["kitten", "2", json.dumps(data)], None, 123, self.boss)

    def test_update_and_expiry_are_scoped_to_workspace_and_keep_values_out_of_panes(self):
        variables = dict(self.manager.tabs[0][0].user_vars)
        with patch.object(self.module.time, "monotonic", return_value=100):
            self.assertTrue(self.update({"cpu_percent": 25, "ram_percent": 75, "age_seconds": 3}))
        data = self.manager._kzw_resource_usage
        self.assertEqual((data["cpu"], data["ram"], data["expires"]), (25, 75, 112))
        self.assertFalse(hasattr(self.other, "_kzw_resource_usage"))
        self.assertEqual(self.manager.tabs[0][0].user_vars, variables)
        callback, delay, repeat = self.fast.add_timer.call_args.args
        self.assertEqual((delay, repeat), (12, False))
        callback(11)
        self.assertIsNone(data["cpu"])
        self.assertIsNone(data["ram"])
        self.assertEqual(self.manager.mark_tab_bar_dirty.call_count, 2)

    def test_old_expiry_cannot_invalidate_new_sample(self):
        self.update({"cpu_percent": 20, "age_seconds": 0})
        old = self.fast.add_timer.call_args.args[0]
        self.update({"cpu_percent": 30, "age_seconds": 0})
        self.fast.remove_timer.assert_called_once_with(11)
        old(11)
        self.assertEqual(self.manager._kzw_resource_usage["cpu"], 30)

    def test_mismatched_origin_workspace_or_unmanaged_window_is_ignored(self):
        self.assertFalse(self.update(origin="elsewhere"))
        self.assertFalse(self.update(workspace="Other"))
        self.manager.tabs[0].append(types.SimpleNamespace(user_vars={}))
        self.assertFalse(self.update())
        self.manager.mark_tab_bar_dirty.assert_not_called()

    def test_utility_tab_does_not_block_host_metrics(self):
        self.manager.tabs.append([types.SimpleNamespace(user_vars={"kzw_utility": "condor-queue"})])
        self.assertTrue(self.update({"cpu_percent": 25, "age_seconds": 0}))
        self.assertEqual(self.manager._kzw_resource_usage["cpu"], 25)

    def test_invalid_stale_or_absent_metrics_show_unknown_and_do_not_schedule_timer(self):
        for sample in (None, {}, {"cpu_percent": 20, "age_seconds": 16}, {"age_seconds": float("nan")}):
            self.update(sample)
            self.assertIsNone(self.manager._kzw_resource_usage["cpu"])
        self.fast.add_timer.assert_not_called()
        self.update({"cpu_percent": float("nan"), "ram_percent": 101, "age_seconds": 0})
        self.assertIsNone(self.manager._kzw_resource_usage["cpu"])
        self.assertIsNone(self.manager._kzw_resource_usage["ram"])

    def test_refresh_only_reloads_the_matching_custom_renderer_once(self):
        module = types.ModuleType("kitty.tab_bar")
        replacement = lambda: None
        replacement._kzw_resources = True
        module.load_custom_draw_tab = Mock(return_value=replacement)
        module.load_custom_draw_tab_module = Mock()
        old = lambda: None
        old._equal_margins = True
        self.manager.tab_bar = types.SimpleNamespace(draw_func=old)
        with patch.dict(sys.modules, {"kitty.tab_bar": module}):
            self.module.refresh_renderer(self.manager)
            self.assertIs(self.manager.tab_bar.draw_func, replacement)
            self.module.refresh_renderer(self.manager)
        module.load_custom_draw_tab_module.clear_cached.assert_called_once_with()
        module.load_custom_draw_tab.clear_cached.assert_called_once_with()
        module.load_custom_draw_tab.assert_called_once_with()
        other = lambda: None
        self.other.tab_bar = types.SimpleNamespace(draw_func=other)
        self.module.refresh_renderer(self.other)
        self.assertIs(self.other.tab_bar.draw_func, other)
