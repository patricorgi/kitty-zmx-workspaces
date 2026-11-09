"""Experimental remote workspace server; newline-delimited JSON over SSH.

This module deliberately does not own PTYs. Remote-origin workspaces use its
revision-checked store and status queries; only explicit close requests can
schedule zmx destruction. The server persists across SSH connections, but
does not yet provide pushed lifecycle events.
"""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
PANE = re.compile(r"^kzw-[0-9a-f]{32}$")
REQUEST = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MAX_REQUEST = 1024 * 1024


class Conflict(ValueError):
    pass


class SessionNotReady(Conflict):
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
            CREATE TABLE IF NOT EXISTS observed_sessions (
                workspace TEXT NOT NULL, pane TEXT NOT NULL,
                PRIMARY KEY(workspace, pane)
            );
            CREATE TABLE IF NOT EXISTS leases (
                workspace TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reservations (
                pane TEXT PRIMARY KEY, workspace TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_presence (
                workspace TEXT NOT NULL, pane TEXT NOT NULL, present INTEGER NOT NULL,
                PRIMARY KEY(workspace, pane)
            );
            CREATE TABLE IF NOT EXISTS workspace_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL,
                revision INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS staged_layouts (
                workspace TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                snapshot TEXT NOT NULL, panes TEXT NOT NULL
            );
        """)
        self.zmx = zmx

    def close(self):
        self.db.close()

    def check_lease(self, name, token):
        row = self.db.execute("SELECT token,expires FROM leases WHERE workspace=?", (name,)).fetchone()
        if row is None:
            if token is not None:
                raise Conflict("workspace lease is not held")
            return
        if token != row[0] or row[1] <= time.time():
            raise Conflict("workspace lease lost or held by another client")

    def add_event(self, name, revision, kind, data):
        self.db.execute("INSERT INTO workspace_events (workspace,revision,kind,data) VALUES (?,?,?,?)",
                        (name, revision, kind, json.dumps(data, separators=(",", ":"))))

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
        if method in ("workspace.lease.acquire", "workspace.lease.renew", "workspace.lease.release", "workspace.lease.inspect"):
            token = req.get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
                raise ValueError("invalid workspace lease token")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if not self.db.execute("SELECT 1 FROM workspaces WHERE name=?", (name,)).fetchone():
                    raise ValueError("workspace does not exist")
                row = self.db.execute("SELECT token,expires FROM leases WHERE workspace=?", (name,)).fetchone()
                now = time.time()
                if method == "workspace.lease.inspect":
                    state = ("none" if row is None else ("own" if row[0] == token else "other") +
                             ("-active" if row[1] > now else "-expired"))
                    self.db.execute("COMMIT")
                    return {"state": state}
                if method == "workspace.lease.acquire":
                    if row and row[0] == token and row[1] > now:
                        self.db.execute("COMMIT")
                        return {"held": True, "ttl": max(0, row[1] - now)}
                    if row and (row[1] > now or row[0] == token):
                        raise Conflict("workspace is attached or lease token is stale")
                    self.db.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?)", (name, token, now + 30))
                elif method == "workspace.lease.release" and row and row[0] == token:
                    # Idempotent even after expiry, but never releases a newer owner.
                    self.db.execute("UPDATE leases SET expires=0 WHERE workspace=?", (name,))
                else:
                    self.check_lease(name, token)
                    self.db.execute("UPDATE leases SET expires=? WHERE workspace=?", (now + 30, name))
                self.db.execute("COMMIT")
                return {"held": method != "workspace.lease.release", "ttl": 30}
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        if method == "workspace.get":
            row = self.db.execute("SELECT revision,snapshot,panes FROM workspaces WHERE name=?", (name,)).fetchone()
            if row is None:
                return {"exists": False, "revision": 0}
            reservations = [r[0] for r in self.db.execute(
                "SELECT pane FROM reservations WHERE workspace=? ORDER BY pane", (name,))]
            staged = self.db.execute("SELECT revision,snapshot,panes FROM staged_layouts WHERE workspace=?", (name,)).fetchone()
            return {"exists": True, "revision": row[0], "snapshot": row[1], "panes": json.loads(row[2]),
                    "reservations": reservations, "staged": ({"revision": staged[0], "snapshot": staged[1],
                                                                "panes": json.loads(staged[2])} if staged else None)}
        if method == "workspace.stage":
            revision, snapshot, panes = req.get("revision"), req.get("snapshot"), req.get("panes")
            if (type(revision) is not int or revision < 0 or not isinstance(snapshot, str) or
                    not isinstance(panes, list) or any(not isinstance(p, str) or not PANE.fullmatch(p) for p in panes) or
                    len(set(panes)) != len(panes)):
                raise ValueError("invalid staged workspace layout")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.check_lease(name, req.get("lease_token"))
                row = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
                if row is None or row[0] != revision:
                    raise Conflict("workspace changed before staging layout")
                old, current = set(json.loads(row[1])), set(panes)
                reservations = {r[0] for r in self.db.execute("SELECT pane FROM reservations WHERE workspace=?", (name,))}
                if not old.issubset(current) or not (current - old) or not (current - old).issubset(reservations):
                    raise Conflict("staged layout must retain owned panes and use reserved additions")
                encoded = json.dumps(sorted(current))
                previous = self.db.execute("SELECT revision,snapshot,panes FROM staged_layouts WHERE workspace=?", (name,)).fetchone()
                if previous and not set(json.loads(previous[2])).issubset(current):
                    raise Conflict("staging would discard panes from an earlier staged layout")
                if previous != (revision, snapshot, encoded):
                    self.db.execute("INSERT OR REPLACE INTO staged_layouts VALUES (?,?,?,?)", (name, revision, snapshot, encoded))
                    self.add_event(name, revision, "layout.staged", {"panes": sorted(current)})
                self.db.execute("COMMIT")
                return {"revision": revision, "panes": sorted(current)}
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        if method == "workspace.sync":
            self.db.execute("BEGIN")
            try:
                snapshot = self.dispatch({"method": "workspace.get", "workspace": name})
                if snapshot["exists"]:
                    snapshot["event_cursor"] = self.db.execute(
                        "SELECT COALESCE(MAX(seq),0) FROM workspace_events WHERE workspace=?", (name,)).fetchone()[0]
                self.db.execute("COMMIT")
                return snapshot
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        if method == "workspace.events":
            after, limit = req.get("after", 0), req.get("limit", 100)
            if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
                raise ValueError("invalid event cursor or limit")
            rows = self.db.execute("SELECT seq,revision,kind,data FROM workspace_events "
                                   "WHERE workspace=? AND seq>? ORDER BY seq LIMIT ?", (name, after, limit)).fetchall()
            return {"events": [{"seq": seq, "revision": rev, "kind": kind, "data": json.loads(data)}
                               for seq, rev, kind, data in rows]}
        if method == "pane.reserve":
            pane, operation, revision = req.get("pane"), req.get("request_id"), req.get("revision")
            if not isinstance(pane, str) or not PANE.fullmatch(pane):
                raise ValueError("invalid pane ID")
            if not isinstance(operation, str) or not REQUEST.fullmatch(operation) or type(revision) is not int or revision < 0:
                raise ValueError("invalid reservation request")
            # A lost reservation acknowledgement can be replayed after a
            # concurrent layout revision; pane identity and operation ID must
            # remain unchanged, and the current lease is checked separately.
            intent = {key: value for key, value in req.items() if key not in ("lease_token", "revision")}
            digest = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.check_lease(name, req.get("lease_token"))
                previous = self.db.execute("SELECT digest,response FROM operations WHERE workspace=? AND id=?", (name, operation)).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise Conflict("request ID already used for a different operation")
                    self.db.execute("COMMIT")
                    return json.loads(previous[1])
                row = self.db.execute("SELECT revision FROM workspaces WHERE name=?", (name,)).fetchone()
                if row is None or row[0] != revision:
                    raise Conflict("workspace changed before pane reservation")
                for workspace, panes in self.db.execute("SELECT name,panes FROM workspaces"):
                    if pane in json.loads(panes):
                        raise Conflict("pane is already owned by a workspace")
                if self.db.execute("SELECT 1 FROM cleanup WHERE pane=?", (pane,)).fetchone():
                    raise Conflict("pane is awaiting cleanup")
                if self.db.execute("SELECT 1 FROM reservations WHERE pane=?", (pane,)).fetchone():
                    raise Conflict("pane is already reserved")
                env = os.environ.copy()
                env.pop("ZMX_SESSION", None)
                env.pop("ZMX_SESSION_PREFIX", None)
                env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
                listing = subprocess.run([self.zmx, "list", "--short"], capture_output=True,
                                         text=True, env=env, timeout=10, check=True)
                if pane in listing.stdout.splitlines():
                    raise Conflict("pane ID already belongs to a live zmx session")
                self.db.execute("INSERT INTO reservations VALUES (?,?)", (pane, name))
                self.add_event(name, revision, "pane.reserved", {"pane": pane})
                result = {"pane": pane, "revision": revision}
                self.db.execute("INSERT INTO operations VALUES (?,?,?,?)", (name, operation, digest, json.dumps(result)))
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        if method == "workspace.watch":
            known = req.get("known")
            if known is not None and not isinstance(known, dict):
                raise ValueError("invalid known workspace status")
            timeout = req.get("timeout", 15)
            if type(timeout) not in (int, float) or not 0 <= timeout <= 20:
                raise ValueError("invalid watch timeout")
            deadline = time.monotonic() + timeout
            while True:
                current = self.dispatch({"method": "workspace.status", "workspace": name})
                if current != known or time.monotonic() >= deadline:
                    return current
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        if method == "workspace.status":
            row = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError("workspace does not exist")
            panes = set(json.loads(row[1]))
            env = os.environ.copy()
            env.pop("ZMX_SESSION", None)
            env.pop("ZMX_SESSION_PREFIX", None)
            env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
            listing = subprocess.run([self.zmx, "list", "--short"], capture_output=True,
                                     text=True, env=env, timeout=10, check=True)
            live = panes.intersection(listing.stdout.splitlines())
            self.db.execute("BEGIN IMMEDIATE")
            try:
                latest = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
                if latest != row:
                    raise Conflict("workspace changed while checking session status")
                seen = {r[0] for r in self.db.execute(
                    "SELECT pane FROM observed_sessions WHERE workspace=?", (name,))}
                presence = {p: present for p, present in self.db.execute(
                    "SELECT pane,present FROM session_presence WHERE workspace=?", (name,))}
                for pane in live - seen:
                    self.db.execute("INSERT INTO observed_sessions VALUES (?,?)", (name, pane))
                seen.update(live)
                for pane in sorted(panes):
                    present = 1 if pane in live else 0
                    if pane in seen and presence.get(pane) != present:
                        self.db.execute("INSERT OR REPLACE INTO session_presence VALUES (?,?,?)", (name, pane, present))
                        self.add_event(name, row[0], "session.live" if present else "session.missing", {"pane": pane})
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return {"revision": row[0], "live": sorted(live), "missing": sorted(panes - live),
                    "confirmed_missing": sorted((panes - live) & seen)}
        if method == "cleanup.list":
            rows = self.db.execute("SELECT pane FROM cleanup WHERE workspace=? ORDER BY pane", (name,)).fetchall()
            return {"panes": [r[0] for r in rows]}
        if method == "cleanup.retry":
            return self.retry_cleanup(name, req.get("lease_token"))
        if method not in ("workspace.put", "workspace.recover-stage", "panes.close", "workspace.prune"):
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
        # The same durable intent may be retried by a newly fenced lease owner.
        # Authorization is checked separately before looking up the operation.
        intent = {key: value for key, value in req.items() if key != "lease_token"}
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.check_lease(name, req.get("lease_token"))
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
            if method in ("workspace.put", "workspace.recover-stage"):
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
            added = current - old
            if method == "workspace.recover-stage":
                staged = self.db.execute("SELECT revision,snapshot,panes FROM staged_layouts WHERE workspace=?", (name,)).fetchone()
                reservations = {r[0] for r in self.db.execute("SELECT pane FROM reservations WHERE workspace=?", (name,))}
                if (not staged or staged[0] != actual or staged[1] != snapshot or
                        set(json.loads(staged[2])) != current or not added or reservations != added):
                    raise Conflict("staged layout or reservations changed before recovery")
            if added:
                # Registration is ownership, not merely a view observation.
                # Never adopt a session that the origin cannot confirm exists.
                env = os.environ.copy()
                env.pop("ZMX_SESSION", None)
                env.pop("ZMX_SESSION_PREFIX", None)
                env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
                listing = subprocess.run([self.zmx, "list", "--short"], capture_output=True,
                                         text=True, env=env, timeout=10, check=True)
                required = current if method == "workspace.recover-stage" else added
                if not required.issubset(set(listing.stdout.splitlines())):
                    raise SessionNotReady("new pane session is missing or not yet ready")
            # Never share ownership: closing one workspace must not kill a pane
            # referenced by another workspace or awaiting cleanup.
            for other, other_panes in self.db.execute("SELECT name,panes FROM workspaces WHERE name<>?", (name,)):
                if current.intersection(json.loads(other_panes)):
                    raise Conflict(f"pane already belongs to workspace {other}")
            pending = {r[0] for r in self.db.execute("SELECT pane FROM cleanup")}
            if current & pending:
                raise Conflict("pane is awaiting cleanup")
            foreign_reservations = {r[0] for r in self.db.execute("SELECT pane FROM reservations WHERE workspace<>?", (name,))}
            if current & foreign_reservations:
                raise Conflict("pane is reserved by another workspace")
            new_revision = actual + 1
            self.db.execute("INSERT OR REPLACE INTO workspaces VALUES (?,?,?,?)", (name, new_revision, snapshot, json.dumps(sorted(current))))
            for pane in added:
                self.db.execute("INSERT OR IGNORE INTO observed_sessions VALUES (?,?)", (name, pane))
                self.db.execute("INSERT OR REPLACE INTO session_presence VALUES (?,?,1)", (name, pane))
                self.db.execute("DELETE FROM reservations WHERE workspace=? AND pane=?", (name, pane))
            for pane in removed:
                self.db.execute("DELETE FROM observed_sessions WHERE workspace=? AND pane=?", (name, pane))
                self.db.execute("DELETE FROM session_presence WHERE workspace=? AND pane=?", (name, pane))
            staged = self.db.execute("SELECT snapshot,panes FROM staged_layouts WHERE workspace=?", (name,)).fetchone()
            if staged and staged[0] == snapshot and set(json.loads(staged[1])) == current:
                self.db.execute("DELETE FROM staged_layouts WHERE workspace=?", (name,))
            self.add_event(name, new_revision, "layout.updated", {"panes": sorted(current), "removed": sorted(removed)})
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

    def retry_cleanup(self, name, token=None):
        # Serialize listing, kill, and acknowledgement. No automatic retries:
        # cleanup occurs only when an authenticated client asks for it.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.check_lease(name, token)
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
                    verification = subprocess.run([self.zmx, "list", "--short"], capture_output=True,
                                                  text=True, env=env, timeout=10, check=True)
                    if pane in verification.stdout.splitlines():
                        failed.append(pane)
                        continue
                self.db.execute("DELETE FROM cleanup WHERE workspace=? AND pane=?", (name, pane))
            self.db.execute("COMMIT")
            return {"remaining": failed}
        except Exception:
            self.db.execute("ROLLBACK")
            raise


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kzw"


def build_id():
    source = os.environ.get("KZW_COORDINATOR_SOURCE")
    if source is None:
        source = Path(__file__).read_bytes()
    else:
        source = source.encode()
    return hashlib.sha256(source).hexdigest()


def response(store, line):
    try:
        result = store.dispatch(json.loads(line))
        return {"ok": True, "result": result}
    except SessionNotReady as exc:
        return {"ok": False, "code": "session_not_ready", "error": str(exc)}
    except (ValueError, KeyError, UnicodeDecodeError, sqlite3.Error, OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}


def serve():
    """Run one per-state-directory coordinator, independent of SSH clients."""
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe workspace server state directory")
    fd = os.open(root / "server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lock = os.fdopen(fd, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return
    path = root / "server.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if path.exists():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("refusing to replace an unowned server socket")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise ValueError("server socket already active")
            finally:
                probe.close()
        old_umask = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(old_umask)
        identity = path.stat().st_ino
        listener.listen(32)
        listener.settimeout(1)
        stopping = threading.Event()
        client_lock = threading.Lock()
        active_clients = [0]

        def handle(client):
            try:
                store = Store(root / "remote.sqlite3")
                try:
                    with client, client.makefile("rb") as stream:
                        while True:
                            line = stream.readline(MAX_REQUEST + 1)
                            if not line:
                                break
                            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                                break
                            try:
                                request = json.loads(line)
                            except (ValueError, UnicodeDecodeError):
                                request = None
                            if request == {"method": "server.info"}:
                                data = json.dumps({"ok": True, "result": {"pid": os.getpid(), "protocol": 1,
                                                                          "build": build_id()}},
                                                  separators=(",", ":")).encode() + b"\n"
                                client.sendall(data)
                                continue
                            if request == {"method": "server.shutdown"}:
                                with client_lock:
                                    if active_clients[0] != 1:
                                        data = b'{"ok":false,"error":"other workspace clients are connected; detach them first"}\n'
                                    else:
                                        stopping.set()
                                        data = b'{"ok":true,"result":{"stopping":true}}\n'
                                client.sendall(data)
                                if stopping.is_set():
                                    break
                                continue
                            data = json.dumps(response(store, line), separators=(",", ":")).encode() + b"\n"
                            client.sendall(data)
                finally:
                    store.close()
            except (OSError, sqlite3.Error):
                client.close()
            finally:
                with client_lock:
                    active_clients[0] -= 1

        while not stopping.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            with client_lock:
                if stopping.is_set():
                    client.close()
                    break
                active_clients[0] += 1
            threading.Thread(target=handle, args=(client,), daemon=True).start()
    finally:
        listener.close()
        if "identity" in locals():
            try:
                if path.lstat().st_ino == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        lock.close()


def connect_server():
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe workspace server state directory")
    path = root / "server.sock"

    def connect():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe coordinator socket")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
            return client
        except OSError:
            client.close()
            raise

    try:
        return connect()
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    source = os.environ.get("KZW_COORDINATOR_SOURCE")
    if source:
        program = "import base64,os;exec(base64.b64decode(os.environ['KZW_COORDINATOR_SOURCE']))"
        command = [sys.executable, "-u", "-c", program, "--serve"]
    else:
        command = [sys.executable, "-u", "-m", "kzw.remote", "--serve"]
    subprocess.Popen(command, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return connect()
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.05)
    raise OSError("workspace server did not start; check remote state directory and socket path")


def main():
    if sys.argv[1:] == ["--serve"]:
        serve()
        return
    client = connect_server()
    try:
        with client.makefile("rb") as stream:
            client.sendall(b'{"method":"server.info"}\n')
            info = json.loads(stream.readline(MAX_REQUEST + 1))
            compatible = (info.get("ok") is True and info.get("result", {}).get("protocol") == 1 and
                          info.get("result", {}).get("build") == build_id())
            while True:
                line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
                if not line:
                    break
                if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                    raise ValueError("request too large or unterminated")
                if not compatible:
                    try:
                        request = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        request = None
                    if request not in ({"method": "server.info"}, {"method": "server.shutdown"}):
                        sys.stdout.buffer.write(b'{"ok":false,"error":"workspace server version differs; stop the old server before retrying"}\n')
                        sys.stdout.buffer.flush()
                        continue
                client.sendall(line)
                reply = stream.readline(MAX_REQUEST + 1)
                if not reply or len(reply) > MAX_REQUEST or not reply.endswith(b"\n"):
                    raise OSError("workspace server disconnected or response too large")
                sys.stdout.buffer.write(reply)
                sys.stdout.buffer.flush()
    finally:
        client.close()


if __name__ == "__main__":
    main()
