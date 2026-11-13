"""Explicit transient utility tabs are views, never workspace terminals."""

from copy import deepcopy
import re


def is_utility(window):
    values = window.get("user_vars", {})
    return (values.get("kzw_utility") == "condor-queue" and
            isinstance(values.get("kzw_utility_source"), str) and
            re.fullmatch(r"[1-9][0-9]*", values["kzw_utility_source"]) is not None and
            isinstance(values.get("kzw_utility_pane"), str) and
            re.fullmatch(r"kzw-[0-9a-f]{32}", values["kzw_utility_pane"]) is not None and
            not values.get("kzw_pane") and not values.get("kzw_workspace"))


def utility_tab(tab):
    windows = tab.get("windows", [])
    marked = any("kzw_utility" in w.get("user_vars", {}) for w in windows)
    if marked and (len(windows) != 1 or not is_utility(windows[0])):
        raise ValueError("invalid or mixed kzw utility tab; refusing an incomplete workspace")
    return marked


def only_utilities(os_window):
    tabs = os_window.get("tabs", [])
    return bool(tabs) and all(utility_tab(tab) for tab in tabs)


def workspace_tree(tree):
    """Exclude only validated utility-only tabs, retaining managed focus.

    Never hide arbitrary unmanaged tabs or a utility mixed with a shell. When
    the utility is selected, its source is the saved focus (if still present).
    No live Kitty state is mutated by this projection.
    """
    result = []
    for os_window in deepcopy(tree):
        tabs = os_window.get("tabs", [])
        utilities = [tab for tab in tabs if utility_tab(tab)]
        if not utilities:
            result.append(os_window)
            continue
        kept = [tab for tab in tabs if tab not in utilities]
        active = next((tab for tab in utilities if tab.get("is_active")), None)
        os_window["tabs"] = kept
        if active and kept:
            values = active["windows"][0]["user_vars"]
            source = next(((tab, w) for tab in kept for w in tab.get("windows", [])
                           if str(w["id"]) == values["kzw_utility_source"] and
                           w.get("user_vars", {}).get("kzw_pane") == values["kzw_utility_pane"]), None)
            selected = source[0] if source else kept[0]
            for tab in kept:
                tab["is_active"] = tab is selected
            if source:
                for window in selected["windows"]:
                    window["is_active"] = window is source[1]
        if kept:
            result.append(os_window)
    return result
