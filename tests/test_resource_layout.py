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
        class KittyTab(list):
            pass

        self.kitty_tab = KittyTab([
            types.SimpleNamespace(id=1, title="bash", user_vars={"kzw_pane": "kzw-" + "a" * 32}),
            types.SimpleNamespace(id=2, title="nvim", user_vars={"kzw_pane": "kzw-" + "b" * 32}),
        ])
        self.kitty_tab.name = "bash · nvim"
        self.kitty_tab.active_window = self.kitty_tab[1]
        fast.get_boss.return_value = types.SimpleNamespace(os_window_map={1: self.manager},
                                                          tab_for_id=lambda tab_id: self.kitty_tab if tab_id == 7 else None)
        class TabData(types.SimpleNamespace):
            def _replace(self, **values):
                return TabData(**{**vars(self), **values})

        self.tab = TabData(os_window_id=1, tab_id=7, title="bash · nvim")
        self.draw_separator = tabs.draw_tab_with_separator

    def test_exact_dotted_layout_and_independent_colors(self):
        segments = self.renderer["resource_segments"](self.tab, 0x9399b2)
        self.assertEqual("".join(text for text, _ in segments), " Test · 󰢹 remote · 󰻠 25% ·  75% ")
        self.assertEqual(segments[0][1], 0xcba6f7)
        self.assertEqual(segments[2][1], 0x89b4fa)
        self.assertEqual(segments[4][1], self.renderer["usage_color"](25))
        self.assertEqual(segments[6][1], self.renderer["usage_color"](75))
        self.assertTrue(all(color == 0x9399b2 for text, color in segments if text == ' · '))

    def test_local_selected_keeps_workspace_metrics_and_privacy(self):
        self.window.user_vars = {"kzw_local": "kzw-" + "c" * 32,
                                 "kzw_local_workspace": "Test", "kzw_local_origin": "remote"}
        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        segments = self.renderer["resource_segments"](self.tab, 0)
        self.assertIn((" ••••", 0xcba6f7), segments)
        self.assertIn(("󰢹 ••••••", 0x89b4fa), segments)
        self.assertIn("25%", "".join(text for text, _ in segments))
        self.window.user_vars["kzw_local_origin"] = "other"
        self.assertEqual(self.renderer["resource_segments"](self.tab, 0), [])

    def test_local_label_is_blue_and_right_aligned_with_no_status_overlap(self):
        local = types.SimpleNamespace(user_vars={
            "kzw_local": "kzw-" + "c" * 32, "kzw_local_owner": "d" * 32})
        self.kitty_tab[:] = [local]
        self.tab.is_active = True
        self.tab.title = "yazi"

        class Screen:
            def __init__(self, columns):
                self.columns = columns
                self.cursor = types.SimpleNamespace(x=15, fg=123, bg=456, bold=False, italic=False)
                self.draws = []

            def draw(self, text):
                self.draws.append((self.cursor.x, text, self.cursor.fg))
                self.cursor.x += len(text)

        data = types.SimpleNamespace(inactive_fg=0x9399b2, default_bg=0x1e1e2e, tab_bar_edge="top")
        screen = Screen(140)
        end = self.renderer["draw_tab"](data, screen, self.tab, 15, 50, 2, True,
                                         types.SimpleNamespace(for_layout=False))
        start = self.manager.tab_bar._kzw_local_start[1]
        labels = [draw for draw in screen.draws if draw[1] == " yazi "]
        self.assertEqual(labels, [(start, " yazi ", 0x89b4fa)])
        self.assertGreater(start, 15)
        self.assertEqual(end, start + 6)
        status_width = sum(len(text) for text, _ in self.renderer["resource_segments"](self.tab, 0))
        self.assertEqual(end + 2, screen.columns - status_width)
        narrow = Screen(25)
        end = self.renderer["draw_tab"](data, narrow, self.tab, 15, 6, 2, True,
                                         types.SimpleNamespace(for_layout=False))
        self.assertEqual(end, 25)
        self.assertNotIn("25%", "".join(text for _, text, _ in narrow.draws))

    def test_connection_states_replace_metrics_in_yellow_without_shortcut_hint(self):
        for state, label in (("stalled", "Connection stalled…"), ("reconnecting", "Reconnecting…"),
                             ("restoring", "Restoring…"), ("connecting", "Connecting…"),
                             ("offline", "Waiting for network…")):
            self.record.update(connection=state, connection_expires=float("inf"))
            segments = self.renderer["resource_segments"](self.tab, 0x9399b2)
            text = "".join(text for text, _ in segments)
            self.assertEqual(text, " Test · 󰢹 remote · " + label + " ")
            self.assertEqual(segments[-2], (label, 0xf9e2af))
            self.assertNotIn("⌘", text)
            self.assertNotIn("%", text)
        self.record["connection"] = ""
        self.assertIn("25%", "".join(text for text, _ in self.renderer["resource_segments"](self.tab, 0)))

    def test_detach_overrides_connection_and_metrics_with_privacy(self):
        self.record.update(connection="offline", connection_expires=float("inf"))
        self.window.user_vars["kzw_connection"] = "reconnecting"
        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        self.manager._kzw_detach_in_progress = {"workspace": "Test", "origin": "remote"}
        segments = self.renderer["resource_segments"](self.tab, 0x9399b2)
        self.assertEqual("".join(text for text, _ in segments), " •••• · 󰢹 •••••• · Detaching… ")
        self.assertEqual(segments[-2], ("Detaching…", 0xf9e2af))
        self.assertNotIn("⌘", "".join(text for text, _ in segments))
        self.manager._kzw_detach_in_progress = None
        self.assertIn(("Waiting for network…", 0xf9e2af), self.renderer["resource_segments"](self.tab, 0))
        self.record["connection"] = ""
        self.window.user_vars["kzw_connection"] = ""
        self.assertIn("25%", "".join(text for text, _ in self.renderer["resource_segments"](self.tab, 0)))

    def test_detach_status_does_not_leak_across_workspace_or_origin(self):
        for workspace, origin in (("Other", "remote"), ("Test", "other")):
            self.manager._kzw_detach_in_progress = {"workspace": workspace, "origin": origin}
            segments = self.renderer["resource_segments"](self.tab, 0)
            self.assertNotIn("Detaching…", "".join(text for text, _ in segments))

    def test_detach_status_visible_while_local_companion_selected(self):
        self.window.user_vars = {"kzw_local": "kzw-" + "c" * 32,
                                 "kzw_local_workspace": "Test", "kzw_local_origin": "remote"}
        self.manager._kzw_detach_in_progress = {"workspace": "Test", "origin": "remote"}
        self.assertIn(("Detaching…", 0xf9e2af), self.renderer["resource_segments"](self.tab, 0))

    def test_stale_health_and_individual_pane_recovery_override_control_success(self):
        self.record.update(connection="", connection_expires=0)
        segments = self.renderer["resource_segments"](self.tab, 0)
        self.assertIn(("Connection stalled…", 0xf9e2af), segments)
        self.record["connection_expires"] = float("inf")
        self.window.user_vars["kzw_connection"] = "reconnecting"
        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        text = "".join(text for text, _ in self.renderer["resource_segments"](self.tab, 0))
        self.assertEqual(text, " •••• · 󰢹 •••••• · Reconnecting… ")

    def test_new_pane_connecting_is_distinct_from_recovery_and_keeps_privacy(self):
        self.record.update(connection="", connection_expires=float("inf"))
        self.window.user_vars["kzw_connection"] = "connecting"
        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        segments = self.renderer["resource_segments"](self.tab, 0)
        self.assertEqual("".join(text for text, _ in segments), " •••• · 󰢹 •••••• · Connecting… ")
        self.assertIn(("Connecting…", 0xf9e2af), segments)
        self.record["connection"] = "reconnecting"
        self.assertIn(("Reconnecting…", 0xf9e2af), self.renderer["resource_segments"](self.tab, 0))

    def test_known_offline_overrides_stale_metrics_and_retry_phase(self):
        self.record.update(connection="offline", connection_expires=float("inf"), expires=0)
        self.window.user_vars["kzw_connection"] = "reconnecting"
        self.window.current_marker_spec = ("function", "helpers/privacy_marker.py")
        segments = self.renderer["resource_segments"](self.tab, 0)
        self.assertEqual("".join(text for text, _ in segments), " •••• · 󰢹 •••••• · Waiting for network… ")
        self.assertIn(("Waiting for network…", 0xf9e2af), segments)

    def test_expired_offline_notification_is_unknown_not_permanent_offline(self):
        self.record.update(connection="offline", connection_expires=0)
        segments = self.renderer["resource_segments"](self.tab, 0)
        self.assertIn(("Connection stalled…", 0xf9e2af), segments)
        self.assertNotIn(("Waiting for network…", 0xf9e2af), segments)

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

    def draw_zoom_title(self, active=False, active_template="{index} {title}"):
        class DrawData(types.SimpleNamespace):
            def _replace(self, **values):
                return DrawData(**{**vars(self), **values})

        data = DrawData(title_template="{index} {title}", active_title_template=active_template,
                        tab_fg=lambda tab: 0xcdd6f4)
        self.tab.is_active = active
        screen = types.SimpleNamespace(cursor=types.SimpleNamespace(bold=False))
        self.renderer["draw_title"](data, screen, self.tab, 0, 80, 1, False,
                                    types.SimpleNamespace(for_layout=False))
        return data, self.draw_separator.call_args.args[0]

    def test_zoom_name_is_black_or_white_bold_without_mutating_title_or_adding_glyph(self):
        self.tab.layout_name, self.tab.num_window_groups = "stack", 2
        for active in (False, True):
            with self.subTest(active=active):
                original, displayed = self.draw_zoom_title(active)
                color = "0;0;0" if active else "255;255;255"
                rendered = self.draw_separator.call_args.args[2].title
                self.assertEqual(rendered, "bash · \x1b[1;38;2;" + color + "mnvim\x1b[22;38;2;205;214;244m")
                self.assertNotIn("󰊓", rendered)
                self.assertEqual(displayed.title_template, original.title_template)
                self.assertEqual(original.title_template, "{index} {title}")
                self.assertEqual(original.active_title_template, "{index} {title}")
                self.assertEqual(self.tab.title, "bash · nvim")

    def test_zoom_indicator_counts_pane_groups_not_overlay_windows(self):
        for layout, groups, windows in (("stack", 1, 3), ("stack", 1, 1), ("splits", 2, 2)):
            with self.subTest(layout=layout, groups=groups, windows=windows):
                self.tab.layout_name, self.tab.num_window_groups, self.tab.num_windows = layout, groups, windows
                original, displayed = self.draw_zoom_title(active_template=None)
                self.assertEqual(displayed.title_template, original.title_template)
                self.assertIsNone(displayed.active_title_template)
                self.assertEqual(self.draw_separator.call_args.args[2].title, self.tab.title)

    def test_zoom_toggle_does_not_retain_or_duplicate_style_and_keeps_fallback_template(self):
        self.tab.num_window_groups = 2
        for layout, expected in (("stack", 1), ("splits", 0), ("stack", 1)):
            self.tab.layout_name = layout
            original, displayed = self.draw_zoom_title(active=True, active_template=None)
            self.assertEqual(self.draw_separator.call_args.args[2].title.count("\x1b[1;38;2;"), expected)
            self.assertEqual(displayed.active_title_template, "{index} {title}")
            self.assertIsNone(original.active_title_template)

    def test_zoom_style_keeps_title_expressions_and_privacy_masking(self):
        self.tab.layout_name, self.tab.num_window_groups = "stack", 2
        self.tab.title = "ssh Test remote · nvim"
        self.kitty_tab.active_window = self.kitty_tab[0]
        self.window.current_marker_spec = ("function", "privacy_marker.py")
        template = " {index} {title.replace(' · ', '·')} "
        _, displayed = self.draw_zoom_title(active=True, active_template=template)
        self.assertEqual(displayed.active_title_template, template.strip())
        self.assertEqual(self.draw_separator.call_args.args[2].title,
                         "\x1b[1;38;2;0;0;0mssh •••• ••••••\x1b[22;38;2;205;214;244m · nvim")
        self.assertEqual(self.tab.title, "ssh Test remote · nvim")

    def test_duplicate_split_names_are_selected_by_window_identity(self):
        self.tab.layout_name, self.tab.num_window_groups, self.tab.title = "stack", 2, "bash · bash"
        for selected in (0, 1, 0):
            self.kitty_tab.active_window = self.kitty_tab[selected]
            self.draw_zoom_title(active=True)
            labels = self.draw_separator.call_args.args[2].title.split(" · ")
            self.assertTrue(labels[selected].startswith("\x1b[1;38;2;0;0;0m"))
            self.assertEqual(labels[1 - selected], "bash")

    def test_command_status_is_not_styled_as_part_of_the_split_name(self):
        self.tab.layout_name, self.tab.num_window_groups, self.tab.title = "stack", 2, "bash · running · nvim"
        self.kitty_tab.active_window = self.kitty_tab[0]
        self.kitty_tab[0].user_vars["kzw_command_state"] = "running"
        self.draw_zoom_title(active=True)
        self.assertEqual(self.draw_separator.call_args.args[2].title,
                         "\x1b[1;38;2;0;0;0mbash\x1b[22;38;2;205;214;244m · running · nvim")

    def test_uncertain_mapping_stays_plain_and_native_active_window_title_is_supported(self):
        self.tab.layout_name, self.tab.num_window_groups = "stack", 2
        for title in ("custom name", "bash · unknown · nvim"):
            self.tab.title = title
            self.draw_zoom_title()
            self.assertEqual(self.draw_separator.call_args.args[2].title, title)
        self.kitty_tab.name = ""
        self.tab.title = self.kitty_tab.active_window.title
        self.draw_zoom_title()
        self.assertEqual(self.draw_separator.call_args.args[2].title,
                         "\x1b[1;38;2;255;255;255mnvim\x1b[22;38;2;205;214;244m")
