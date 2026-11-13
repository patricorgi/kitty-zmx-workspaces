"""Cache host metrics in Kitty, with a redraw when the reading expires."""

import json
import math
import re
import time
import weakref

from kittens.tui.handler import result_handler
from kitty.fast_data_types import add_timer, remove_timer

STALE_AFTER = 15.0
PANE = re.compile(r"^kzw-[0-9a-f]{32}$")


def main(args):
    pass


def percent(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100 else None


def refresh_renderer(manager):
    bar = getattr(manager, "tab_bar", None)
    draw = getattr(bar, "draw_func", None)
    if (getattr(draw, "_equal_margins", False) and not getattr(draw, "_kzw_resources", False)
            and not getattr(bar, "_kzw_renderer_checked", False)):
        bar._kzw_renderer_checked = True
        try:
            from kitty.tab_bar import load_custom_draw_tab, load_custom_draw_tab_module
            load_custom_draw_tab_module.clear_cached()
            load_custom_draw_tab.clear_cached()
            bar.draw_func = load_custom_draw_tab()
        except (ImportError, AttributeError):
            pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    if len(args) != 3:
        raise ValueError("usage: metrics_kitten.py OS_WINDOW_ID PAYLOAD")
    manager = boss.os_window_map.get(int(args[1]))
    data = json.loads(args[2])
    if manager is None:
        return False
    windows = [window for tab in manager.tabs for window in tab if not window.user_vars.get("kzw_utility")]
    if not windows or any(not PANE.fullmatch(window.user_vars.get("kzw_pane", "")) or
                          window.user_vars.get("kzw_workspace") != data["workspace"] or
                          window.user_vars.get("kzw_origin", "") != data["origin"] for window in windows):
        return False
    sample = data.get("metrics")
    sample = sample if isinstance(sample, dict) else {}
    host = data["origin"] or sample.get("host") or "local"
    host = re.sub(r"[^A-Za-z0-9_.@:-]", "?", str(host))[:40]
    age = sample.get("age_seconds")
    valid = type(age) in (int, float) and math.isfinite(age) and 0 <= age < STALE_AFTER
    record = {"workspace": data["workspace"], "origin": data["origin"], "host": host,
              "cpu": percent(sample.get("cpu_percent")) if valid else None,
              "ram": percent(sample.get("ram_percent")) if valid else None,
              "expires": time.monotonic() + STALE_AFTER - age if valid else 0}
    previous = getattr(manager, "_kzw_resource_usage", {})
    if previous.get("timer") is not None:
        remove_timer(previous["timer"])
    manager._kzw_resource_usage = record
    refresh_renderer(manager)
    if valid:
        reference = weakref.ref(manager)

        def expire(timer_id):
            current = reference()
            if current is not None and getattr(current, "_kzw_resource_usage", None) is record:
                record["cpu"] = record["ram"] = None
                record.pop("timer", None)
                current.mark_tab_bar_dirty()

        record["timer"] = add_timer(expire, STALE_AFTER - age, False)
    manager.mark_tab_bar_dirty()
    return True
