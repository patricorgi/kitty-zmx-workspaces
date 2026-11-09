"""Experimental remote workspace store; newline-delimited JSON over SSH.

This module deliberately does not own PTYs. It is not yet wired into the Kitty
supervisor. Only explicit close requests can schedule zmx destruction.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
PANE = re.compile(r"^kzw-[0-9a-f]{32}$")
REQUEST = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MAX_REQUEST = 1024 * 1024


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path, zmx="zmx"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workspaces (
                name TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                snapshot TEXT NOT NULL, panes TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                workspace TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL,
                response TEXT NOT NULL, PRIMARY KEY(workspace, id)
            );
            CREATE TABLE IF NOT EXISTS cleanup (
                workspace TEXT NOT NULL, pane TEXT NOT NULL,
                PRIMARY KEY(workspace, pane)
            );
        """)
        self.zmx = zmx

    def close(self):
        self.db.close()

    def dispatch(self, req):
        if not isinstance(req, dict):
            raise ValueError("request must be an object")
        method = req.get("method")
        if method == "workspace.list":
            rows = self.db.execute("SELECT name,revision FROM workspaces ORDER BY name").fetchall()
            return {"workspaces": [{"name": row[0], "revision": row[1]} for row in rows]}
        name = req.get("workspace", "")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("invalid workspace")
        if method == "workspace.get":
            row = self.db.execute("SELECT revision,snapshot,panes FROM workspaces WHERE name=?", (name,)).fetchone()
            if row is None:
                return {"exists": False, "revision": 0}
            return {"exists": True, "revision": row[0], "snapshot": row[1], "panes": json.loads(row[2])}
        if method == "cleanup.list":
            rows = self.db.execute("SELECT pane FROM cleanup WHERE workspace=? ORDER BY pane", (name,)).fetchall()
            return {"panes": [r[0] for r in rows]}
        if method == "cleanup.retry":
            return self.retry_cleanup(name)
        if method not in ("workspace.put", "panes.close", "workspace.prune"):
            raise ValueError("unknown method")
        request_id = req.get("request_id", "")
        if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
            raise ValueError("invalid request ID")
        revision = req.get("revision")
        snapshot = req.get("snapshot")
        panes = req.get("panes")
        if type(revision) is not int or revision < 0 or not isinstance(snapshot, str):
            raise ValueError("invalid revision or snapshot")
        if not isinstance(panes, list) or any(not isinstance(p, str) or not PANE.fullmatch(p) for p in panes):
            raise ValueError("invalid pane list")
        if len(set(panes)) != len(panes):
            raise ValueError("duplicate pane ID")
        removals = req.get("remove", [])
        if not isinstance(removals, list) or any(not isinstance(p, str) or not PANE.fullmatch(p) for p in removals):
            raise ValueError("invalid removal list")
        if len(removals) != len(set(removals)):
            raise ValueError("duplicate removal")
        digest = hashlib.sha256(json.dumps(req, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute("SELECT digest,response FROM operations WHERE workspace=? AND id=?", (name, request_id)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise Conflict("request ID already used for a different operation")
                self.db.execute("COMMIT")
                return json.loads(previous[1])
            row = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
            actual, old = (row[0], set(json.loads(row[1]))) if row else (0, set())
            if revision != actual:
                raise Conflict(f"revision mismatch: expected {revision}, current {actual}")
            current, removed = set(panes), set(removals)
            if method == "workspace.put":
                if removed or not old.issubset(current):
                    raise ValueError("workspace.put cannot remove panes")
            elif not removed or removed != old - current or current - old:
                raise ValueError("close/prune must remove exactly the specified existing panes")
            if method == "workspace.prune":
                # An attachment/host failure is not a shell exit. Confirm on
                # the origin itself before forgetting any session.
                env = os.environ.copy()
                env.pop("ZMX_SESSION", None)
                env.pop("ZMX_SESSION_PREFIX", None)
                env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
                listing = subprocess.run([self.zmx, "list", "--short"], capture_output=True,
                                         text=True, env=env, timeout=10, check=True)
                if removed.intersection(listing.stdout.splitlines()):
                    raise Conflict("a pruned zmx session is still alive")
            # Never share ownership: closing one workspace must not kill a pane
            # referenced by another workspace or awaiting cleanup.
            for other, other_panes in self.db.execute("SELECT name,panes FROM workspaces WHERE name<>?", (name,)):
                if current.intersection(json.loads(other_panes)):
                    raise Conflict(f"pane already belongs to workspace {other}")
            pending = {r[0] for r in self.db.execute("SELECT pane FROM cleanup")}
            if current & pending:
                raise Conflict("pane is awaiting cleanup")
            new_revision = actual + 1
            self.db.execute("INSERT OR REPLACE INTO workspaces VALUES (?,?,?,?)", (name, new_revision, snapshot, json.dumps(sorted(current))))
            if method == "panes.close":
                for pane in removed:
                    self.db.execute("INSERT INTO cleanup VALUES (?,?)", (name, pane))
            response = {"revision": new_revision, "pending_cleanup": sorted(removed) if method == "panes.close" else []}
            self.db.execute("INSERT INTO operations VALUES (?,?,?,?)", (name, request_id, digest, json.dumps(response)))
            self.db.execute("COMMIT")
            return response
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def retry_cleanup(self, name):
        # Serialize listing, kill, and acknowledgement. No automatic retries:
        # cleanup occurs only when an authenticated client asks for it.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            pending = [r[0] for r in self.db.execute("SELECT pane FROM cleanup WHERE workspace=?", (name,))]
            env = os.environ.copy()
            env.pop("ZMX_SESSION", None)
            env.pop("ZMX_SESSION_PREFIX", None)
            env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
            result = subprocess.run([self.zmx, "list", "--short"], capture_output=True, text=True, env=env, timeout=10, check=True)
            live = set(result.stdout.splitlines())
            failed = []
            for pane in pending:
                if pane in live:
                    result = subprocess.run([self.zmx, "kill", pane], capture_output=True, text=True, env=env, timeout=10)
                    if result.returncode:
                        failed.append(pane)
                        continue
                self.db.execute("DELETE FROM cleanup WHERE workspace=? AND pane=?", (name, pane))
            self.db.execute("COMMIT")
            return {"remaining": failed}
        except Exception:
            self.db.execute("ROLLBACK")
            raise


def main():
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    store = Store(root / "kzw" / "remote.sqlite3")
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
            if not line:
                break
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                print(json.dumps({"ok": False, "error": "request too large or unterminated"}), flush=True)
                break
            try:
                result = store.dispatch(json.loads(line))
                response = {"ok": True, "result": result}
            except (ValueError, KeyError, sqlite3.Error, OSError, subprocess.SubprocessError) as exc:
                response = {"ok": False, "error": str(exc)}
            print(json.dumps(response, separators=(",", ":")), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
