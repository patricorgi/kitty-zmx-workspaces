"""Exercise real Kitty marker actions on synthetic panes, never user panes."""

import json
import runpy
import time
from types import MethodType, SimpleNamespace

from kittens.tui.handler import result_handler
from kitty.fast_data_types import Screen
from kitty.tab_bar import ExtraData, TabBar, as_rgb
from kitty.window import Window


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    # Loading a custom renderer installs class hooks. Restore every pre-test
    # hook before returning; the live renderer is refreshed separately.
    actions = {name: getattr(Window, name) for name in ("toggle_marker", "set_marker", "remove_marker")}
    layout, update = TabBar.layout, TabBar.update_horizontal
    try:
        module = runpy.run_path(args[1])
        fixture = next(manager for manager in boss.os_window_map.values() if manager.tab_bar_data)
        prototype = fixture.tab_bar_data[0]
        data = fixture.tab_bar.draw_data._replace(title_template=" {index} {title} ",
                                                  active_title_template=" {index} {title} ")
        screen = Screen(None, 1, 160)
        windows = [SimpleNamespace(os_window_id=-123, screen=Screen(None, 2, 80), current_marker_spec=None,
                                    user_vars={"kzw_workspace": "Test", "kzw_origin": "hidden-host"}) for _ in range(2)]
        tabs = [prototype._replace(title=title, os_window_id=-123, is_active=i == 0)
                for i, title in enumerate(("private-first-title", "ssh hidden-host"))]
        count = [0]

        def render():
            count[0] += 1
            screen.cursor.x = 0
            screen.erase_in_line(2, False)
            for index, tab in enumerate(tabs):
                screen.cursor.fg = as_rgb(data.tab_fg(tab))
                screen.cursor.bg = as_rgb(data.tab_bg(tab))
                module["draw_tab"](data, screen, tab, screen.cursor.x, 45, index + 1,
                                   index == len(tabs) - 1, ExtraData())

        record = {"workspace": "Test", "origin": "hidden-host", "host": "hidden-host",
                  "cpu": 25, "ram": 75, "expires": time.monotonic() + 100}
        manager = SimpleNamespace(tabs=[[window] for window in windows],
                                  active_tab=SimpleNamespace(active_window=windows[0]),
                                  tab_bar=SimpleNamespace(draw_func=module["draw_tab"]),
                                  _kzw_resource_usage=record, update_tab_bar_data=render,
                                  mark_tab_bar_dirty=lambda: None)
        module["privacy_enabled"].__globals__["get_boss"] = lambda: SimpleNamespace(os_window_map={-123: manager})
        for window in windows:
            window.remove_marker = MethodType(Window.remove_marker, window)
        render()
        baseline = str(screen.line(0))
        assert "private-first-title" in baseline and "ssh hidden-host" in baseline and "hidden-host" in baseline
        marker = args[2]

        def masked():
            line = str(screen.line(0))
            assert "hidden-host" not in line and "Test" not in line, line
            assert line == baseline.replace("hidden-host", "•" * len("hidden-host")).replace("Test", "••••"), line
            assert all(text in line for text in ("private-first-title", "ssh ", "󰻠", "")), line
            return line.strip()

        Window.toggle_marker(windows[1], "function", marker, 0)
        hidden = masked()  # An inactive private pane masks host/workspace names.
        Window.toggle_marker(windows[0], "function", marker, 0)
        Window.toggle_marker(windows[1], "function", marker, 0)
        masked()          # One remaining private pane still protects the bar.
        Window.toggle_marker(windows[0], "function", marker, 0)
        assert str(screen.line(0)) == baseline
        Window.set_marker(windows[1], ["function", marker])
        masked()
        Window.remove_marker(windows[1])
        assert str(screen.line(0)) == baseline
        return json.dumps({"passed": True, "masked_line": hidden, "redraws": count[0],
                           "restored": True, "user_panes_modified": False})
    finally:
        for name, action in actions.items():
            setattr(Window, name, action)
        TabBar.layout, TabBar.update_horizontal = layout, update
