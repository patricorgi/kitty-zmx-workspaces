"""Portable layouts for coordinator protocol tests (real session-ID leaves)."""

import json
import shlex


def layout(*panes, label="Test"):
    if not panes:
        return "# kzw-empty-workspace\n"
    lines = ["new_tab " + label, "layout splits"]
    for wid, pane in enumerate(panes, 1):
        lines.append(shlex.join(["launch", "kitty-unserialize-data=" + json.dumps({"id": wid}),
                                 "--var=kzw_pane=" + pane, "--var=kzw_host=KZW_ORIGIN",
                                 "KZW_PANE_EXECUTABLE", "--pane", pane, "--host", "KZW_ORIGIN", "--existing"]))
    return "\n".join(lines) + "\n"


def split_layout(a, b, c):
    """Two tabs with nonsequential Kitty IDs, unequal splits and stable focus."""
    state = {"class": "Splits", "opts": {},
             "pairs": {"one": 7, "two": 8, "horizontal": True, "bias": 0.37},
             "all_windows": {"active_group_idx": 1, "active_group_history": [91, 17],
                             "window_groups": [{"id": 7, "window_ids": [17]}, {"id": 8, "window_ids": [91]}]}}
    first = layout(a, b).replace('"id": 1', '"id": 17').replace('"id": 2', '"id": 91')
    first = first.replace("layout splits\n", "layout splits\nset_layout_state " + json.dumps(state) + "\n")
    second = layout(c, label="Second").replace('"id": 1', '"id": 33')
    return '# kzw-focus ' + json.dumps({"pane": b}) + '\n' + first + second + 'focus_tab 1\n'
