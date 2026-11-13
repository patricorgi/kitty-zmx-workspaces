"""Optional unit coverage of the user's custom Kitty resource renderer."""

import os
from pathlib import Path
import runpy
import sys
import types
import unittest
from unittest.mock import Mock, patch


class ResourceLayoutTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ.get("KZW_TEST_TAB_BAR", Path.home() / ".config/kitty/tab_bar.py"))
        if not path.is_file():
            self.skipTest("compatible custom tab bar is not installed")
        fast = types.ModuleType("kitty.fast_data_types")
        for name in ("add_timer", "get_boss", "get_options", "mark_tab_bar_dirty",
                     "set_tab_bar_render_data", "viewport_for_window", "wcswidth"):
            setattr(fast, name, Mock())
        fast.wcswidth.side_effect = len
        tabs = types.ModuleType("kitty.tab_bar")
        tabs.TabBar = types.SimpleNamespace(layout=lambda *a: None, update_horizontal=lambda *a: None)
        tabs.as_rgb = lambda value: value
        tabs.draw_tab_with_separator = Mock()
        windows = types.ModuleType("kitty.window")
        windows.Window = type("Window", (), {name: lambda *a, **kw: None for name in
                                            ("toggle_marker", "set_marker", "remove_marker")})
        with patch.dict(sys.modules, {"kitty": types.ModuleType("kitty"),
                                      "kitty.fast_data_types": fast, "kitty.tab_bar": tabs, "kitty.window": windows}):
            self.renderer = runpy.run_path(str(path))
        if "WORKSPACE_ICON" not in self.renderer:
            self.skipTest("custom tab bar predates workspace/host icons")
        self.record = {"workspace": "Test", "origin": "remote", "host": "remote",
                       "cpu": 25, "ram": 75, "expires": float("inf")}
        self.window = types.SimpleNamespace(user_vars={"kzw_workspace": "Test", "kzw_origin": "remote"},
                                             os_window_id=1, current_marker_spec=None)
        self.manager = types.SimpleNamespace(_kzw_resource_usage=self.record,
                                             active_tab=types.SimpleNamespace(active_window=self.window),
                                             tabs=[[self.window]], update_tab_bar_data=Mock(), mark_tab_bar_dirty=Mock(),
                                             tab_bar=types.SimpleNamespace(draw_func=self.renderer["draw_tab"]))
        fast.get_boss.return_value = types.SimpleNamespace(os_window_map={1: self.manager})
        class TabData(types.SimpleNamespace):
            def _replace(self, **values):
                return TabData(**{**vars(self), **values})

        self.tab = TabData(os_window_id=1, title="bash · nvim")
        self.draw_separator = tabs.draw_tab_with_separator

    def test_exact_dotted_layout_and_independent_colors(self):
        segments = self.renderer["resource_segments"](self.tab, 0x9399b2)
        self.assertEqual("".join(text for text, _ in segments), " Test · 󰢹 remote · 󰻠 25% ·  75% ")
        self.assertEqual(segments[0][1], 0xcba6f7)
        self.assertEqual(segments[2][1], 0x89b4fa)
        self.assertEqual(segments[4][1], self.renderer["usage_color"](25))
        self.assertEqual(segments[6][1], self.renderer["usage_color"](75))
        self.assertTrue(all(color == 0x9399b2 for text, color in segments if text == ' · '))

    def test_stale_readings_keep_identity_and_neutral_unknown_icons(self):
        self.record["expires"] = 0
        segments = self.renderer["resource_segments"](self.tab, 0x9399b2)
        self.assertEqual("".join(text for text, _ in segments), " Test · 󰢹 remote · 󰻠 — ·  — ")
        self.assertEqual([segments[0][1], segments[2][1]], [0xcba6f7, 0x89b4fa])
        self.assertTrue(all(color == 0x9399b2 for _, color in segments[3:]))

    def test_different_workspace_or_origin_does_not_receive_status(self):
        for key in ("kzw_workspace", "kzw_origin"):
            previous = self.window.user_vars[key]
            self.window.user_vars[key] = "Other"
            self.assertEqual(self.renderer["resource_segments"](self.tab, 0x9399b2), [])
            self.window.user_vars[key] = previous

    def test_privacy_marker_in_inactive_pane_masks_identity_without_layout_shift(self):
        baseline = self.renderer["resource_segments"](self.tab, 0x9399b2)
        other = types.SimpleNamespace(current_marker_spec=("function", "helpers/privacy_marker.py"))
        self.manager.tabs.append([other])
        self.assertTrue(self.renderer["privacy_enabled"](1))
        masked = self.renderer["resource_segments"](self.tab, 0x9399b2)
        self.assertEqual("".join(text for text, _ in masked), " •••• · 󰢹 •••••• · 󰻠 25% ·  75% ")
        self.assertEqual([color for _, color in baseline], [color for _, color in masked])
        self.assertEqual([len(text) for text, _ in baseline], [len(text) for text, _ in masked])
        other.current_marker_spec = None
        self.assertFalse(self.renderer["privacy_enabled"](1))
        self.assertEqual(self.renderer["resource_segments"](self.tab, 0x9399b2), baseline)

    def test_other_marker_types_or_windows_do_not_hide_this_window(self):
        for spec in (None, ("regex", [[1, "secret"]]), ("function", "highlight.py")):
            self.window.current_marker_spec = spec
            self.assertFalse(self.renderer["privacy_enabled"](1))
        self.window.current_marker_spec = ("function", "/private/helpers/privacy_marker.py")
        self.assertTrue(self.renderer["privacy_enabled"](1))
        self.assertFalse(self.renderer["privacy_enabled"](2))

    def test_privacy_keeps_title_templates_and_nonhost_text(self):
        class DrawData(types.SimpleNamespace):
            def _replace(self, **values):
                return DrawData(**{**vars(self), **values})

        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        data = DrawData(title_template="{title}", active_title_template="{title}")
        self.tab.is_active = False
        self.renderer["draw_title"](data, Mock(), self.tab, 0, 20, 1, False, types.SimpleNamespace(for_layout=False))
        displayed = self.draw_separator.call_args.args[0]
        self.assertEqual((displayed.title_template, displayed.active_title_template), ("{title}", "{title}"))
        self.assertEqual(self.draw_separator.call_args.args[2].title, "bash · nvim")
        self.assertEqual(data.title_template, "{title}")

    def test_hostname_in_tab_title_is_masked_without_changing_saved_title(self):
        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        self.tab.title = "ssh remote"
        self.tab.is_active = False
        self.renderer["draw_title"](Mock(), Mock(), self.tab, 0, 20, 1, False, types.SimpleNamespace(for_layout=False))
        self.assertEqual(self.draw_separator.call_args.args[2].title, "ssh ••••••")
        self.assertEqual(self.tab.title, "ssh remote")

    def test_workspace_in_tab_title_is_masked_without_changing_saved_title(self):
        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        self.tab.title = "Test bash remote"
        self.tab.is_active = False
        self.renderer["draw_title"](Mock(), Mock(), self.tab, 0, 20, 1, False, types.SimpleNamespace(for_layout=False))
        self.assertEqual(self.draw_separator.call_args.args[2].title, "•••• bash ••••••")
        self.assertEqual(self.tab.title, "Test bash remote")

    def test_overlapping_workspace_and_hostname_are_masked_longest_first(self):
        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        self.record.update(workspace="remote", host="remote-host")
        self.assertEqual(self.renderer["mask_identity"]("remote remote-host", 1), "•••••• •••••••••••")

    def test_marker_toggle_redraws_but_does_not_change_shortcuts_or_other_windows(self):
        def toggle(window):
            window.current_marker_spec = None if window.current_marker_spec else ("function", "privacy_marker.py")
            return "done"

        wrapped = self.renderer["redraw_after_marker_change"](toggle)
        self.assertEqual(wrapped(self.window), "done")
        self.assertTrue(self.renderer["privacy_enabled"](1))
        wrapped(self.window)
        self.assertFalse(self.renderer["privacy_enabled"](1))
        self.assertEqual(self.manager.update_tab_bar_data.call_count, 2)
        self.assertEqual(self.manager.mark_tab_bar_dirty.call_count, 2)
        self.assertIs(wrapped._privacy_native, toggle)
