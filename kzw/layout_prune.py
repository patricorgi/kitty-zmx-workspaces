"""Conservatively remove absent panes from serialized Kitty split layouts."""

import json
import shlex

from .core import FLAG, FOCUS_PREFIX, PANE, saved_focus


def prune_snapshot(snapshot, missing):
    """Return a pruned snapshot; reject formats whose topology is uncertain.

    This is a pure transformation. Callers must independently verify session
    absence and commit the result using the coordinator's revision fence.
    """
    missing = set(missing)
    if not missing or any(not PANE.fullmatch(p) for p in missing):
        raise ValueError("invalid missing pane list")
    focus = saved_focus(snapshot)
    tabs, header = [], []
    for line in snapshot.splitlines():
        if line == "new_tab" or line.startswith("new_tab "):
            tabs.append([line])
        elif tabs:
            tabs[-1].append(line)
        elif not line.startswith(FOCUS_PREFIX):
            header.append(line)
    if not tabs:
        raise ValueError("snapshot has no serialized tabs")
    seen, surviving, output = set(), [], []
    focus_neighbor = None
    for lines in tabs:
        state_lines = [i for i, line in enumerate(lines) if line.startswith("set_layout_state ")]
        layouts = [line for line in lines if line.startswith("layout ")]
        launches = {}
        for i, line in enumerate(lines):
            if not line.startswith("launch "):
                continue
            args = shlex.split(line)
            if len(args) < 2 or not args[1].startswith(FLAG):
                raise ValueError("unsupported serialized launch")
            wid = json.loads(args[1][len(FLAG):]).get("id")
            panes = [arg[len("--var=kzw_pane="):] for arg in args if arg.startswith("--var=kzw_pane=")]
            if type(wid) is not int or len(panes) != 1 or not PANE.fullmatch(panes[0]) or wid in launches or panes[0] in seen:
                raise ValueError("invalid serialized pane identity")
            launches[wid] = (i, panes[0])
            seen.add(panes[0])
        removed_ids = {wid for wid, (_, pane) in launches.items() if pane in missing}
        if not removed_ids:
            output.append(lines)
            surviving.extend(pane for _, pane in launches.values())
            continue
        if not launches or len(state_lines) != 1 or layouts != ["layout splits"]:
            raise ValueError("cannot safely prune this Kitty layout")
        state = json.loads(lines[state_lines[0]][len("set_layout_state "):])
        if not isinstance(state, dict) or state.get("class") != "Splits":
            raise ValueError("unsupported split layout state")
        all_windows = state.get("all_windows")
        if not isinstance(all_windows, dict) or not isinstance(all_windows.get("window_groups"), list):
            raise ValueError("invalid Kitty window groups")
        groups = all_windows["window_groups"]
        if any(not isinstance(g, dict) or type(g.get("id")) is not int or
               not isinstance(g.get("window_ids"), list) or len(g["window_ids"]) != 1 or
               g["window_ids"][0] not in launches for g in groups):
            raise ValueError("unsupported Kitty window groups")
        if len(groups) != len(launches) or len({g["id"] for g in groups}) != len(groups) or len({g["window_ids"][0] for g in groups}) != len(groups):
            raise ValueError("ambiguous Kitty window groups")
        removed_groups = {g["id"] for g in groups if g["window_ids"][0] in removed_ids}
        group_ids = {g["id"] for g in groups}
        visited = set()

        def nearest(node, target):
            if type(node) is int:
                return node == target, None if node in removed_groups else node
            if not isinstance(node, dict):
                raise ValueError("invalid Kitty split tree")
            left_found, left = nearest(node["one"], target) if "one" in node else (False, None)
            right_found, right = nearest(node["two"], target) if "two" in node else (False, None)
            if left_found:
                return True, left if left is not None else right
            if right_found:
                return True, right if right is not None else left
            return False, left if left is not None else right

        focused_group = next((g['id'] for g in groups if launches[g['window_ids'][0]][1] == focus), None)
        if focused_group in removed_groups:
            _, neighbor = nearest(state.get('pairs'), focused_group)
            focus_neighbor = next((launches[g['window_ids'][0]][1] for g in groups if g['id'] == neighbor), None)

        def prune_pair(node):
            if type(node) is int:
                if node not in group_ids or node in visited:
                    raise ValueError("invalid Kitty split tree")
                visited.add(node)
                return None if node in removed_groups else node
            if not isinstance(node, dict) or set(node) - {"horizontal", "bias", "one", "two"}:
                raise ValueError("unsupported Kitty split tree")
            one = prune_pair(node["one"]) if "one" in node else None
            two = prune_pair(node["two"]) if "two" in node else None
            if one is None:
                return two
            if two is None:
                return one
            result = {k: v for k, v in node.items() if k not in ("one", "two")}
            result.update(one=one, two=two)
            return result

        original_pairs = state.get("pairs")
        root = prune_pair(original_pairs)
        if visited != group_ids:
            raise ValueError("incomplete Kitty split tree")
        if root is None:
            continue
        state["pairs"] = {"one": root} if type(root) is int else root
        kept = [g for g in groups if g["id"] not in removed_groups]
        active = all_windows.get("active_group_idx", 0)
        if type(active) is not int or not 0 <= active < len(groups):
            raise ValueError("invalid Kitty active group")
        chosen = groups[active]["id"]
        if chosen in removed_groups:
            _, neighbor = nearest(original_pairs, chosen)
            chosen = neighbor if neighbor in {g["id"] for g in kept} else kept[0]["id"]
        all_windows["window_groups"] = kept
        all_windows["active_group_idx"] = next(i for i, g in enumerate(kept) if g["id"] == chosen)
        if "active_group_history" in all_windows:
            history = all_windows["active_group_history"]
            if not isinstance(history, list) or any(type(x) is not int for x in history):
                raise ValueError("invalid Kitty focus history")
            all_windows["active_group_history"] = [x for x in history if x in group_ids - removed_groups]
        remove_lines = {launches[wid][0] for wid in removed_ids}
        lines[state_lines[0]] = "set_layout_state " + json.dumps(state, separators=(",", ":"))
        output.append([line for i, line in enumerate(lines) if i not in remove_lines])
        surviving.extend(pane for wid, (_, pane) in launches.items() if wid not in removed_ids)
    if not missing.issubset(seen):
        raise ValueError("missing pane not found in snapshot")
    if not surviving:
        return "# kzw-empty-workspace\n"
    if focus not in surviving:
        focus = focus_neighbor if focus_neighbor in surviving else surviving[0]
    return FOCUS_PREFIX + json.dumps({"pane": focus}, separators=(",", ":")) + "\n" + "\n".join(header + [line for tab in output for line in tab]) + "\n"
