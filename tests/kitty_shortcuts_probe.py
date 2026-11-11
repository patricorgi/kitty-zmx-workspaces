"""In-Kitty inspection and synthetic paste for an explicitly marked test reader."""

import json

from kittens.tui.handler import result_handler
from kitty.boss import get_options
from kitty.fast_data_types import KeyEvent


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    if len(args) > 1:
        from unittest.mock import patch

        window = boss.window_id_map[target_window_id]
        if (len(args) != 3 or args[1] != "paste" or not args[2] or
                window.user_vars.get("kzw_paste_probe") != args[2]):
            raise ValueError("paste probe requires its disposable marked reader")
        # Exercise Kitty's shortcut dispatcher and paste transport without
        # reading or changing the user's clipboard or synthesizing OS events.
        with patch("kitty.keys.grab_keyboard", return_value=True), \
             patch("kitty.boss.get_clipboard_string", return_value="kzw-paste-probe"), \
             patch.object(boss, "window_for_dispatch", window):
            return json.dumps({"consumed": boss.mappings.dispatch_possible_special_key(KeyEvent(key=118, mods=8))})
    options = get_options()
    return json.dumps({
        "global": {name: str(key) for name, key in boss.mappings.global_shortcuts.items()},
        "modes": {name: {str(key): [definition.definition for definition in definitions]
                         for key, definitions in mode.keymap.items()}
                  for name, mode in boss.mappings.keyboard_modes.items()},
        "templates": [options.tab_title_template, options.active_tab_title_template],
        "renderers": {str(manager.os_window_id): [manager.tab_bar.draw_data.title_template,
                                                manager.tab_bar.draw_data.active_title_template]
                      for manager in boss.os_window_map.values()},
    })
