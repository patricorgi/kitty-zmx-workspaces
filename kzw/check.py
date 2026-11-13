"""Report origin inventory/ownership mismatches without changing workspace state."""

import json


def summarize(inventory):
    available = inventory["available"]
    sessions, workspaces = inventory["sessions"], inventory["workspaces"]
    stray = sorted(s["id"] for s in sessions if s["presence"] == "live" and not s["references"]) if available else None
    missing = sorted(s["id"] for s in sessions if s["presence"] == "missing" and
                     any(ref["role"] == "layout" for ref in s["references"])) if available else None
    conflicts = sorted(s["id"] for s in sessions if s["assignment"] == "conflict")
    errors = {name: data["layout_error"] for name, data in sorted(workspaces.items()) if "layout_error" in data}
    pending = []
    for session in sessions:
        owners = {ref["workspace"] for ref in session["references"] if ref["role"] == "layout"}
        if any(ref["role"] in ("reserved", "closing") or (ref["role"] == "staged" and ref["workspace"] not in owners)
               for ref in session["references"]):
            pending.append(session["id"])
    return {
        "status": "incomplete" if not available else "issues" if stray or missing or conflicts or errors else "ok",
        "live_sessions": sum(s["presence"] == "live" for s in sessions) if available else None,
        "workspace_count": len(workspaces),
        "stray_sessions": stray,
        "missing_sessions": missing,
        "conflicting_sessions": conflicts,
        "invalid_workspaces": errors,
        "pending_sessions": sorted(pending),
    }


def display(value):
    value = str(value)
    return json.dumps(value, ensure_ascii=False) if any(ord(c) < 32 or ord(c) == 127 for c in value) else value


def render(report):
    check = report["check"]
    live = check["live_sessions"] if report["available"] else "unknown"
    lines = [f"Check {display(report['origin'] or 'local')}: {check['status']}",
             f"Live zmx sessions: {live}; workspaces: {check['workspace_count']}",
             "Source: " + json.dumps(report["observation"]["source"], sort_keys=True)]
    if not report["available"]:
        lines.append("Inventory unavailable: " + display(report["error"]))
        if report["query_source"] != report["observation"]["source"]:
            lines.append("Requested source: " + json.dumps(report["query_source"], sort_keys=True))
    rows = []
    referenced = set()
    for session in sorted(report["sessions"], key=lambda s: (bool(s["references"]), s["workspace"] or "", s["id"])):
        owners = sorted({ref["workspace"] for ref in session["references"]})
        referenced.update(owners)
        roles = ",".join(sorted({ref["role"] for ref in session["references"]})) or "-"
        status = session["presence"]
        if session["id"] in (check["stray_sessions"] or []):
            status = "stray"
        if session["assignment"] == "conflict":
            status += "/conflict"
        rows.append([status, ",".join(owners) or "(unassigned)", roles, session["id"]])
    for name in sorted(set(report["workspaces"]) - referenced):
        rows.append(["-", name, "-", "(no session references)"])
    if rows:
        table = [["STATUS", "WORKSPACE", "REFERENCES", "ZMX SESSION"]] + [[display(cell) for cell in row] for row in rows]
        widths = [max(len(row[col]) for row in table) for col in range(3)]
        lines.append("")
        lines.extend("  ".join(row[col].ljust(widths[col]) for col in range(3)) + "  " + row[3] for row in table)
    for name, error in check["invalid_workspaces"].items():
        lines.append(f"Invalid layout {display(name)}: {display(error)}")
    stray = len(check["stray_sessions"]) if report["available"] else "unknown"
    missing = len(check["missing_sessions"]) if report["available"] else "unknown"
    lines += ["", f"Stray: {stray}; missing saved: {missing}; conflicts: {len(check['conflicting_sessions'])}; "
              f"invalid layouts: {len(check['invalid_workspaces'])}; pending: {len(check['pending_sessions'])}"]
    return "\n".join(lines)
