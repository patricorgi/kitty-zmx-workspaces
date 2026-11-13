"""Explicit, lease-fenced demo destruction using the origin's authority Store.

Uploaded over SSH, not installed. Does not restart the running coordinator or
change its protocol. Pending demo creation is converted to durable cleanup in
one SQLite transaction, never inferred from an absent terminal or failed query.
"""

import json
import os
from pathlib import Path
import stat
import sys
import time


def claims(store):
    result = {}
    for name, encoded in store.db.execute("SELECT name,panes FROM workspaces"):
        for pane in json.loads(encoded):
            result.setdefault(pane, set()).add(name)
    for name, encoded in store.db.execute("SELECT workspace,panes FROM staged_layouts"):
        for pane in json.loads(encoded):
            result.setdefault(pane, set()).add(name)
    for table in ("reservations", "cleanup"):
        for name, pane in store.db.execute("SELECT workspace,pane FROM " + table):
            result.setdefault(pane, set()).add(name)
    return result


def inspect(store, name):
    inventory = store.inventory()
    if not inventory["available"]:
        raise ValueError("origin inventory is unavailable; nothing may be removed")
    state = store.dispatch({"method": "workspace.get", "workspace": name})
    owned = []
    for session in inventory["sessions"]:
        owners = {ref["workspace"] for ref in session["references"]}
        if name in owners:
            if owners != {name}:
                raise ValueError("demo session has conflicting ownership")
            owned.append(session["id"])
    return {"exists": state["exists"], "revision": state["revision"], "owned": sorted(owned),
            "inventory": inventory}


def destroy(store, request):
    name, revision, token = request["name"], request["revision"], request["token"]
    expected = set(request["owned"])
    store.db.execute("BEGIN IMMEDIATE")
    try:
        row = store.db.execute("SELECT revision FROM workspaces WHERE name=?", (name,)).fetchone()
        if row is None:
            if expected:
                raise ValueError("workspace disappeared after ownership confirmation; retry inspection")
            store.db.execute("COMMIT")
            return {"removed": True, "sessions": []}
        if row[0] != revision:
            raise ValueError("demo revision changed; cleanup refused")
        ownership = claims(store)
        owned = {pane for pane, owners in ownership.items() if name in owners}
        if owned != expected or any(ownership[pane] != {name} for pane in owned):
            raise ValueError("demo ownership changed; cleanup refused")
        lease = store.db.execute("SELECT token,expires FROM leases WHERE workspace=?", (name,)).fetchone()
        allowed = {token, *request.get("previous_tokens", [])}
        if lease and lease[1] > time.time() and lease[0] not in allowed:
            raise ValueError("demo is leased by another client; detach it first")
        # A successful, namespace-pinned origin query is required before intent.
        store.read_inventory()
        store.db.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?)", (name, token, time.time() + 120))
        store.check_lease(name, token)
        killing = store.db.execute("SELECT 1 FROM workspace_kills WHERE workspace=?", (name,)).fetchone()
        if not killing:
            new_revision = revision + 1
            store.db.execute("UPDATE workspaces SET revision=?,snapshot=?,panes=? WHERE name=?",
                             (new_revision, "# kzw-empty-workspace\n", "[]", name))
            store.db.execute("INSERT INTO workspace_kills VALUES (?)", (name,))
            for pane in sorted(owned):
                store.db.execute("INSERT OR IGNORE INTO cleanup VALUES (?,?)", (name, pane))
            # Explicit replacement authorizes cancellation, including sessions
            # that were reserved/staged but never committed to a displayed layout.
            store.db.execute("DELETE FROM reservations WHERE workspace=?", (name,))
            store.db.execute("DELETE FROM staged_layouts WHERE workspace=?", (name,))
            store.add_event(name, new_revision, "workspace.kill_requested",
                            {"panes": sorted(owned), "reason": "explicit_demo_replacement",
                             "request_id": request["request_id"]})
        store.db.execute("COMMIT")
    except Exception:
        store.db.execute("ROLLBACK")
        raise
    # Reuse the coordinator's exact-session kill, inventory verification and
    # durable retry implementation. No raw prefix-based zmx kill is performed.
    result = store.retry_cleanup(name, token)
    if result["remaining"]:
        raise ValueError("demo cleanup is pending; durable intent retained for retry")
    store.dispatch({"method": "workspace.lease.release", "workspace": name, "token": token})
    try:
        status = store.dispatch({"method": "workspace.status", "workspace": name})
        store.retire_if_empty(name, status)
    except ValueError:
        # The normal background coordinator may have retired this now-empty
        # workspace immediately after our lease release.
        if store.dispatch({"method": "workspace.get", "workspace": name})["exists"]:
            raise
    if store.dispatch({"method": "workspace.get", "workspace": name})["exists"]:
        raise ValueError("demo retirement is incomplete; do not recreate it")
    after = store.inventory()
    if not after["available"] or any(item["id"] in expected and item["presence"] == "live" for item in after["sessions"]):
        raise ValueError("demo session removal could not be verified")
    return {"removed": True, "sessions": sorted(expected), "inventory": after}


def main():
    request = json.load(sys.stdin)
    namespace = {"__name__": "demo_authority", "__file__": "<shipped-kzw-authority>"}
    os.environ.pop("KZW_LOCAL_COORDINATOR", None)
    if request.get("state_home"):
        if not request["state_home"].startswith("/"):
            raise ValueError("invalid origin state home")
        os.environ["XDG_STATE_HOME"] = request["state_home"]
    exec(compile(request.pop("authority"), "<shipped-kzw-authority>", "exec"), namespace)
    if not namespace["NAME"].fullmatch(request["name"]):
        raise ValueError("invalid demo name")
    if request["action"] not in ("inspect", "destroy"):
        raise ValueError("invalid reset action")
    if request["action"] == "destroy":
        if (not namespace["REQUEST"].fullmatch(request["token"]) or
                not namespace["REQUEST"].fullmatch(request["request_id"]) or
                any(not namespace["PANE"].fullmatch(pane) for pane in request["owned"])):
            raise ValueError("invalid destruction fence")
    database = namespace["state_root"]() / "remote.sqlite3"
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe origin coordinator database")
    store = namespace["Store"](database)
    try:
        result = inspect(store, request["name"]) if request["action"] == "inspect" else destroy(store, request)
        print(json.dumps(result))
    finally:
        store.close()


if __name__ == "__main__":
    main()
