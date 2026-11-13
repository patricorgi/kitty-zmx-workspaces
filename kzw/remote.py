"""Experimental remote workspace server; newline-delimited JSON over SSH.

This module deliberately does not own PTYs. Remote-origin workspaces use its
revision-checked store and status queries; only explicit close requests can
schedule zmx destruction. The server persists across SSH connections.
"""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
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


class HostMetrics:
    """One in-memory, demand-driven sample for all clients of this coordinator."""

    interval = 5.0

    def __init__(self):
        self.lock = threading.Lock()
        self.previous = None
        self.sample = None
        self.sampled = None
        self.libc = None

    def counters(self):
        if sys.platform.startswith("linux"):
            with open("/proc/stat") as stream:
                fields = stream.readline().split()
            if fields[0] != "cpu" or len(fields) < 5:
                raise ValueError("invalid aggregate CPU counters")
            ticks = [int(value) for value in fields[1:9]]
            # Guest times are already included in user/nice; iowait is not busy.
            total = sum(ticks)
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
            with open("/proc/meminfo") as stream:
                memory = {key: int(value.split()[0]) for key, value in
                          (line.split(":", 1) for line in stream)}
            return total, idle, memory["MemTotal"], memory["MemAvailable"]
        if sys.platform == "darwin":
            import ctypes
            if self.libc is None:
                self.libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
                self.libc.mach_host_self.restype = ctypes.c_uint
                self.host_port = self.libc.mach_host_self()

            def statistics(flavor):
                values = (ctypes.c_uint32 * 64)()
                count = ctypes.c_uint(64)
                if self.libc.host_statistics(self.host_port, flavor, values, ctypes.byref(count)):
                    raise OSError("host statistics unavailable")
                return list(values)[:count.value]

            def sysctl(name):
                value = ctypes.c_uint64()
                size = ctypes.c_size_t(ctypes.sizeof(value))
                if self.libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0):
                    raise OSError("memory size unavailable")
                return value.value

            cpu = statistics(3)  # HOST_CPU_LOAD_INFO: user, system, idle, nice
            vm = statistics(2)   # HOST_VM_INFO: free, active, inactive, wired, ...
            if len(cpu) < 4 or len(vm) < 4:
                raise ValueError("incomplete host statistics")
            # Inactive pages are reclaimable. This is an availability estimate,
            # not macOS's memory-pressure indicator or a sum of process RSS.
            return sum(cpu[:4]), cpu[2], sysctl(b"hw.memsize"), (vm[0] + vm[2]) * os.sysconf("SC_PAGE_SIZE")
        raise OSError("host metrics are unsupported on this platform")

    def get(self):
        with self.lock:
            now = time.monotonic()
            if self.sampled is None or now - self.sampled >= self.interval:
                cpu = ram = None
                try:
                    total, idle, memory, available = self.counters()
                    if memory <= 0 or not 0 <= available <= memory or not 0 <= idle <= total:
                        raise ValueError("invalid machine counters")
                    ram = round(100 * (memory - available) / memory, 1)
                    if self.previous is not None:
                        delta = total - self.previous[0]
                        idle_delta = idle - self.previous[1]
                        if delta > 0 and 0 <= idle_delta <= delta:
                            cpu = round(100 * (delta - idle_delta) / delta, 1)
                    self.previous = (total, idle)
                except (OSError, ValueError, KeyError, IndexError):
                    self.previous = None
                self.sample = {"host": socket.gethostname(), "cpu_percent": cpu, "ram_percent": ram}
                self.sampled = now
            return {**self.sample, "age_seconds": max(0, now - self.sampled)}


HOST_METRICS = HostMetrics()


class Conflict(ValueError):
    pass


class SessionNotReady(Conflict):
    pass


def zmx_exit_receipt(directory, pane):
    """Read zmx 0.5.0's final PTY teardown, never terminal output.

    In v0.5.0 main.zig, the deleting-socket record follows waitpid(shell, 0).
    A PTY EOF alone is insufficient (zmx also maps read errors to EOF). Require
    the complete ordered shutdown suffix and independently absent PID/socket.
    Reading only the current log rejects rotation through this short suffix;
    ordinary earlier rotation is fine. A later generation invalidates the suffix.
    """
    path = directory / "logs" / (pane + ".log")
    for parent in (directory, path.parent):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe zmx log directory")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022:
            raise ValueError("unsafe zmx session log")
        stream.seek(max(0, before.st_size - 16384))
        tail = stream.read(16384)
        after = os.fstat(stream.fileno())
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if signature(before) != signature(after) or signature(after) != signature(path.lstat()):
        return None
    # No arbitrary text between the lifecycle records. Warnings are the exact
    # optional killpg failures from handleKill, not general daemon errors.
    prefix = rb"\[(\d+)\] \[info\] \(default\): "
    name = pane.encode()
    pattern = (prefix + rb"shell exited pty_fd=(\d+)\n" +
               prefix + rb"kill received session=" + name + rb"\n" +
               prefix + rb"shutting down daemon session=" + name + rb"\n" +
               prefix + rb"sending SIGHUP session=" + name + rb" pid=(\d+)\n" +
               rb"(?:\[\d+\] \[warning\] \(default\): failed to send SIG(?:HUP|KILL) to pty child err=[A-Za-z]+\n)*" +
               prefix + rb"deleting socket file session=" + name + rb"\n\Z")
    match = re.search(rb"(?:\A|\n)" + pattern, tail)
    if match is None:
        return None
    exited, fd, killed, shutdown, hup, pid, reaped = map(int, match.groups())
    if not 0 < exited <= killed <= shutdown <= hup <= reaped or pid <= 1:
        return None
    # lexists semantics: a stale, inaccessible or symlink socket is unknown.
    try:
        (directory / pane).lstat()
    except FileNotFoundError:
        pass
    else:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        return None
    return {"kind": "zmx-0.5.0-reaped", "pane": pane, "shell_pid": pid,
            "exited_at_ms": exited, "reaped_at_ms": reaped,
            "lifecycle_sha256": hashlib.sha256(match.group().lstrip(b"\n")).hexdigest()}


def layout_sessions(snapshot, panes):
    """Validate the session-ID join at the authority, not only in the UI client.

    Keep this stdlib-only: this module is also shipped as a standalone SSH server.
    Kitty's serialized layout retains topology/sizes; these locations index its
    leaves by persistent zmx ID. Serialized window IDs are layout-local only.
    """
    if (not isinstance(snapshot, str) or not isinstance(panes, list) or
            any(not isinstance(pane, str) or not PANE.fullmatch(pane) for pane in panes) or
            len(panes) != len(set(panes))):
        raise ValueError("invalid layout session manifest")
    if snapshot in ("# kzw-dormant-workspace\n", "# kzw-empty-workspace\n"):
        if panes:
            raise ValueError("empty layout conflicts with pane manifest")
        return {}
    found, windows, focus = {}, set(), None
    states = {}
    tab, split = 0, 0
    for raw in snapshot.splitlines():
        line = raw.strip()
        directive = line.split(maxsplit=1)[0] if line else ""
        if line.startswith("# kzw-focus "):
            value = json.loads(line[len("# kzw-focus "):])
            if focus is not None or not isinstance(value, dict) or value.get("pane") not in panes:
                raise ValueError("invalid layout focus")
            focus = value["pane"]
        elif directive == "new_tab":
            if tab and not split:
                raise ValueError("layout contains an empty tab")
            tab, split = tab + 1, 0
        elif directive == "set_layout_state":
            if not tab or tab in states or len(line.split(maxsplit=1)) != 2:
                raise ValueError("invalid tab layout state")
            states[tab] = json.loads(line.split(maxsplit=1)[1])
        elif directive == "new_os_window":
            raise ValueError("workspace layout must describe one OS window")
        elif directive == "launch":
            args = shlex.split(line)
            command = args[-6:]
            if (not tab or len(args) < 8 or not args[1].startswith("kitty-unserialize-data=") or
                    command[:2] != ["KZW_PANE_EXECUTABLE", "--pane"] or
                    not PANE.fullmatch(command[2]) or command[3:] != ["--host", "KZW_ORIGIN", "--existing"]):
                raise ValueError("layout contains an unmanaged launch")
            pane = command[2]
            data = json.loads(args[1][len("kitty-unserialize-data="):])
            wid = data.get("id") if isinstance(data, dict) else None
            if type(wid) is not int or wid in windows or "cmd_at_shell_startup" in data:
                raise ValueError("invalid serialized layout window")
            if pane in found:
                raise ValueError("duplicate layout session ID")
            for option in args[2:-6]:
                if not option.startswith("--"):
                    raise ValueError("invalid layout launch option")
                if option.startswith("--var=kzw_pane=") and option != "--var=kzw_pane=" + pane:
                    raise ValueError("layout pane identity disagrees with session ID")
                if option.startswith("--var=kzw_host=") and option != "--var=kzw_host=KZW_ORIGIN":
                    raise ValueError("layout origin disagrees with session origin")
            split += 1
            found[pane] = {"tab": tab, "pane": split, "serialized_window_id": wid}
            windows.add(wid)
    if not found or not split or set(found) != set(panes):
        raise ValueError("layout and pane manifest disagree")
    for pane, location in found.items():
        location["focused"] = pane == focus
    for tab, state in states.items():
        all_windows = state.get("all_windows") if isinstance(state, dict) else None
        groups = all_windows.get("window_groups") if isinstance(all_windows, dict) else None
        if not isinstance(groups, list) or any(not isinstance(group, dict) or
                type(group.get("id")) is not int or not isinstance(group.get("window_ids"), list) or
                not group["window_ids"] for group in groups):
            raise ValueError("invalid layout window groups")
        group_ids = [group["id"] for group in groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("duplicate layout window group")
        grouped = [wid for group in groups for wid in group["window_ids"]]
        expected = [loc["serialized_window_id"] for loc in found.values() if loc["tab"] == tab]
        if any(type(wid) is not int for wid in grouped) or sorted(grouped) != sorted(expected):
            raise ValueError("layout topology and session windows disagree")
        if state.get("class") == "Splits":
            stack, leaves = [state.get("pairs")], []
            while stack:
                node = stack.pop()
                if type(node) is int:
                    leaves.append(node)
                elif isinstance(node, dict) and "one" in node:
                    stack.append(node["one"])
                    if "two" in node:
                        stack.append(node["two"])
                else:
                    raise ValueError("invalid split layout tree")
            if sorted(leaves) != sorted(group_ids):
                raise ValueError("split tree and session groups disagree")
    return found


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
            CREATE TABLE IF NOT EXISTS retired_workspaces (
                name TEXT PRIMARY KEY, revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_kills (
                workspace TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS workspace_renames (
                workspace TEXT NOT NULL, id TEXT NOT NULL, destination TEXT NOT NULL,
                revision INTEGER NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(workspace, id)
            );
            CREATE TABLE IF NOT EXISTS zmx_inventory (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                generation INTEGER NOT NULL, observed_at REAL NOT NULL,
                source TEXT NOT NULL, sessions TEXT NOT NULL
            );
        """)
        self.zmx = zmx

    def zmx_environment(self):
        env = os.environ.copy()
        env.pop("ZMX_SESSION", None)
        env.pop("ZMX_SESSION_PREFIX", None)
        env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
        return env

    def inventory_source(self, env):
        selector = next((key for key in ("ZMX_DIR", "XDG_RUNTIME_DIR", "TMPDIR") if env.get(key)), None)
        return {"host": socket.gethostname(), "uid": os.getuid(),
                "executable": shutil.which(self.zmx, path=env["PATH"]) or self.zmx,
                "socket_selector": selector, "socket_base": os.path.abspath(env[selector] if selector else "/tmp")}

    def read_inventory(self):
        """One full, unprefixed zmx inventory, pinned to its origin namespace.

        Serialize sampling with publication so a slow old response cannot replace
        a newer observation. A failed query leaves the last successful sample.
        Callers already in a mutation transaction own its commit/rollback.
        """
        own_transaction = not self.db.in_transaction
        if own_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            env = self.zmx_environment()
            source = self.inventory_source(env)
            previous = self.db.execute("SELECT generation,source FROM zmx_inventory WHERE singleton=1").fetchone()
            if previous and json.loads(previous[1]) != source:
                raise Conflict("zmx inventory origin changed; retained inventory and layouts require investigation")
            listing = subprocess.run([self.zmx, "list", "--short"], capture_output=True,
                                     text=True, env=env, timeout=10, check=True)
            sessions = listing.stdout.splitlines()
            if any(not session or "\x00" in session for session in sessions) or len(sessions) != len(set(sessions)):
                raise ValueError("invalid flattened zmx inventory")
            result = {"generation": previous[0] + 1 if previous else 1, "observed_at": time.time(),
                      "source": source, "sessions": sorted(sessions)}
            self.db.execute("INSERT OR REPLACE INTO zmx_inventory VALUES (1,?,?,?,?)",
                            (result["generation"], result["observed_at"], json.dumps(source), json.dumps(result["sessions"])))
            if own_transaction:
                self.db.execute("COMMIT")
            return result
        except Exception:
            if own_transaction:
                self.db.execute("ROLLBACK")
            raise

    def inventory(self):
        """Account for every live session, plus retained absent/unknown references."""
        error = None
        try:
            sample = self.read_inventory()
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
            row = self.db.execute("SELECT generation,observed_at,source,sessions FROM zmx_inventory WHERE singleton=1").fetchone()
            sample = ({"generation": row[0], "observed_at": row[1], "source": json.loads(row[2]),
                       "sessions": json.loads(row[3])} if row else
                      {"generation": 0, "observed_at": None, "source": None, "sessions": []})
        live = set(sample["sessions"])
        claims = {pane: [] for pane in live}
        workspaces = {}
        self.db.execute("BEGIN")
        try:
            for name, revision, snapshot, encoded in self.db.execute("SELECT name,revision,snapshot,panes FROM workspaces ORDER BY name"):
                panes = json.loads(encoded)
                workspace = {"revision": revision, "snapshot": snapshot, "panes": panes}
                workspaces[name] = workspace
                try:
                    locations = layout_sessions(snapshot, panes)
                except ValueError as exc:
                    # Old/corrupt records remain inspectable, never silently repaired.
                    workspace["layout_error"] = str(exc)
                    locations = {}
                for pane in panes:
                    claims.setdefault(pane, []).append({"workspace": name, "role": "layout", "location": locations.get(pane)})
            for name, revision, snapshot, encoded in self.db.execute("SELECT workspace,revision,snapshot,panes FROM staged_layouts"):
                workspaces[name]["staged"] = {"revision": revision, "snapshot": snapshot, "panes": json.loads(encoded)}
                for pane in json.loads(encoded):
                    claims.setdefault(pane, []).append({"workspace": name, "role": "staged"})
            for table, role in (("reservations", "reserved"), ("cleanup", "closing")):
                for name, pane in self.db.execute(f"SELECT workspace,pane FROM {table}"):
                    claims.setdefault(pane, []).append({"workspace": name, "role": role})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        sessions = []
        for pane, references in sorted(claims.items()):
            owners = {ref["workspace"] for ref in references}
            sessions.append({"id": pane, "presence": "unknown" if error is not None else "live" if pane in live else "missing",
                             "last_seen_live": pane in live, "workspace": next(iter(owners)) if len(owners) == 1 else None,
                             "assignment": "conflict" if len(owners) > 1 else "assigned" if owners else "unassigned",
                             "references": references})
        return {"available": error is None, "error": error,
                "query_source": self.inventory_source(self.zmx_environment()), "observation": sample,
                "sessions": sessions, "workspaces": workspaces}

    def exit_receipts(self, panes):
        """Origin-side positive evidence; inventory can veto, never authorize."""
        sample = self.read_inventory()
        candidates = set(panes) - set(sample["sessions"])
        if not candidates:
            return {}
        env = self.zmx_environment()
        version = subprocess.run([self.zmx, "version"], capture_output=True, text=True,
                                 env=env, timeout=10, check=True)
        fields = dict(line.split(None, 1) for line in version.stdout.splitlines() if line.strip())
        directory = Path(env["ZMX_DIR"] if "ZMX_DIR" in env else
                         str(Path(env["XDG_RUNTIME_DIR"]) / "zmx") if "XDG_RUNTIME_DIR" in env else
                         str(Path(env.get("TMPDIR", "/tmp")) / ("zmx-" + str(os.getuid()))))
        if (fields.get("zmx") != "0.5.0" or fields.get("socket_dir") != str(directory) or
                fields.get("log_dir") != str(directory / "logs")):
            return {}
        receipts = {}
        for pane in sorted(candidates):
            try:
                receipt = zmx_exit_receipt(directory, pane)
            except (OSError, ValueError):
                continue  # Missing, unreadable or ambiguous evidence is unknown.
            if receipt is not None:
                receipts[pane] = {**receipt, "source": sample["source"]}
        return receipts

    def close(self):
        self.db.close()

    def observe_detached(self):
        """Journal status even when no client is connected; never prune here."""
        names = [row[0] for row in self.db.execute("SELECT name FROM workspaces ORDER BY name")]
        if not names:
            try:
                self.read_inventory()
            except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
                pass
        for name in names:
            try:
                if self.db.execute("SELECT 1 FROM cleanup WHERE workspace=?", (name,)).fetchone():
                    self.retry_cleanup(name, unattended=True)
                status = self.dispatch({"method": "workspace.status", "workspace": name})
                self.retire_if_empty(name, status)
            except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
                # Failure or a concurrent mutation is unknown; retry later.
                continue

    def retire_if_empty(self, name, status):
        """Retire an explicitly killed workspace after cleanup and lease release."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
            if not row or row[0] != status["revision"]:
                self.db.execute("COMMIT")
                return False
            panes = set(json.loads(row[1]))
            if panes != set(status["confirmed_missing"]) or status["live"]:
                self.db.execute("COMMIT")
                return False
            busy = any(self.db.execute(query, (name,)).fetchone() for query in (
                "SELECT 1 FROM leases WHERE workspace=? AND expires > strftime('%s','now')",
                "SELECT 1 FROM reservations WHERE workspace=?",
                "SELECT 1 FROM staged_layouts WHERE workspace=?",
                "SELECT 1 FROM cleanup WHERE workspace=?"))
            killing = self.db.execute("SELECT 1 FROM workspace_kills WHERE workspace=?", (name,)).fetchone()
            # Inventory describes existence, not permission to discard layout.
            if not killing or busy:
                self.db.execute("COMMIT")
                return False
            revision = row[0] + 1
            self.add_event(name, revision, "workspace.retired", {"reason": "managed_kill", "panes": sorted(panes)})
            self.db.execute("INSERT OR REPLACE INTO retired_workspaces VALUES (?,?)", (name, revision))
            self.db.execute("DELETE FROM workspace_kills WHERE workspace=?", (name,))
            for table in ("workspaces", "leases", "observed_sessions", "session_presence"):
                column = "name" if table == "workspaces" else "workspace"
                self.db.execute(f"DELETE FROM {table} WHERE {column}=?", (name,))
            self.db.execute("COMMIT")
            return True
        except Exception:
            self.db.execute("ROLLBACK")
            raise

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
        if method == "host.metrics":
            return HOST_METRICS.get()
        if method == "workspace.list":
            rows = self.db.execute("SELECT name,revision FROM workspaces ORDER BY name").fetchall()
            return {"workspaces": [{"name": row[0], "revision": row[1]} for row in rows]}
        if method == "session.inventory":
            return self.inventory()
        name = req.get("workspace", "")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("invalid workspace")
        if method == "workspace.rename":
            return self.rename_workspace(name, req)
        killing = self.db.execute("SELECT 1 FROM workspace_kills WHERE workspace=?", (name,)).fetchone()
        if killing and method not in ("workspace.get", "workspace.sync", "workspace.events", "workspace.events.wait",
                                      "workspace.status", "workspace.watch", "cleanup.list", "cleanup.retry",
                                      "workspace.lease.release"):
            raise Conflict("workspace termination is pending")
        if method == "workspace.kill":
            revision, token = req.get("revision"), req.get("lease_token")
            if type(revision) is not int or revision < 0 or not isinstance(token, str):
                raise ValueError("invalid workspace kill request")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.check_lease(name, token)
                row = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
                if row is None or row[0] != revision:
                    raise Conflict("workspace changed after confirmation")
                if any(self.db.execute(query, (name,)).fetchone() for query in (
                        "SELECT 1 FROM reservations WHERE workspace=?",
                        "SELECT 1 FROM staged_layouts WHERE workspace=?",
                        "SELECT 1 FROM cleanup WHERE workspace=?")):
                    raise Conflict("workspace has unfinished operations; resolve them before killing it")
                panes = json.loads(row[1])
                new_revision = revision + 1
                self.db.execute("UPDATE workspaces SET revision=?,snapshot=?,panes=? WHERE name=?",
                                (new_revision, "# kzw-empty-workspace\n", "[]", name))
                self.db.execute("INSERT INTO workspace_kills VALUES (?)", (name,))
                for pane in panes:
                    self.db.execute("INSERT INTO cleanup VALUES (?,?)", (name, pane))
                self.add_event(name, new_revision, "workspace.kill_requested", {"panes": panes})
                self.db.execute("COMMIT")
                return {"revision": new_revision, "pending_cleanup": panes}
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        if method in ("workspace.lease.acquire", "workspace.lease.renew", "workspace.lease.release",
                      "workspace.lease.inspect", "workspace.lease.recover"):
            token = req.get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
                raise ValueError("invalid workspace lease token")
            if method == "workspace.lease.recover":
                previous = req.get("previous_token")
                if not isinstance(previous, str) or not re.fullmatch(r"[0-9a-f]{32}", previous) or previous == token:
                    raise ValueError("invalid previous workspace lease token")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if not self.db.execute("SELECT 1 FROM workspaces WHERE name=?", (name,)).fetchone():
                    raise ValueError("workspace does not exist")
                if method != "workspace.lease.release" and self.db.execute(
                        "SELECT 1 FROM workspace_kills WHERE workspace=?", (name,)).fetchone():
                    raise Conflict("workspace termination is pending")
                row = self.db.execute("SELECT token,expires FROM leases WHERE workspace=?", (name,)).fetchone()
                now = time.time()
                if method == "workspace.lease.inspect":
                    state = ("none" if row is None else ("own" if row[0] == token else "other") +
                             ("-active" if row[1] > now else "-expired"))
                    self.db.execute("COMMIT")
                    return {"state": state}
                if method == "workspace.lease.recover":
                    # A lost acknowledgement can be retried, but an expired
                    # successor is acknowledged without reviving it. A later
                    # recovery must rotate that token again. Explicit release
                    # (expires=0) and any intervening owner always fence us.
                    if row and row[0] == token and row[1] > 0:
                        self.db.execute("COMMIT")
                        return {"held": row[1] > now, "ttl": max(0, row[1] - now)}
                    if not row or row[0] != previous or not 0 < row[1] <= now:
                        raise Conflict("lease recovery refused: previous lease is active, released, or superseded")
                    self.db.execute("UPDATE leases SET token=?,expires=? WHERE workspace=?", (token, now + 30, name))
                elif method == "workspace.lease.acquire":
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
                retired = self.db.execute("SELECT revision FROM retired_workspaces WHERE name=?", (name,)).fetchone()
                return {"exists": False, "revision": retired[0] if retired else 0}
            reservations = [r[0] for r in self.db.execute(
                "SELECT pane FROM reservations WHERE workspace=? ORDER BY pane", (name,))]
            staged = self.db.execute("SELECT revision,snapshot,panes FROM staged_layouts WHERE workspace=?", (name,)).fetchone()
            return {"exists": True, "revision": row[0], "snapshot": row[1], "panes": json.loads(row[2]),
                    "reservations": reservations, "staged": ({"revision": staged[0], "snapshot": staged[1],
                                                                "panes": json.loads(staged[2])} if staged else None)}
        if method == "workspace.exits":
            panes = req.get("panes")
            if (not isinstance(panes, list) or not panes or
                    any(not isinstance(p, str) or not PANE.fullmatch(p) for p in panes) or len(panes) != len(set(panes))):
                raise ValueError("invalid exit candidates")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if not isinstance(req.get("lease_token"), str):
                    raise Conflict("exit reconciliation requires a lease")
                self.check_lease(name, req["lease_token"])
                row = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
                if not row or row[0] != req.get("revision") or not set(panes).issubset(json.loads(row[1])):
                    raise Conflict("exit candidates differ from the owned layout")
                receipts = self.exit_receipts(panes)
                self.db.execute("COMMIT")
                return {"revision": row[0], "receipts": receipts}
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        if method == "workspace.stage":
            revision, snapshot, panes = req.get("revision"), req.get("snapshot"), req.get("panes")
            if (type(revision) is not int or revision < 0 or not isinstance(snapshot, str) or
                    not isinstance(panes, list) or any(not isinstance(p, str) or not PANE.fullmatch(p) for p in panes) or
                    len(set(panes)) != len(panes)):
                raise ValueError("invalid staged workspace layout")
            layout_sessions(snapshot, panes)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.check_lease(name, req.get("lease_token"))
                if self.db.execute("SELECT 1 FROM workspace_kills WHERE workspace=?", (name,)).fetchone():
                    raise Conflict("workspace termination is pending")
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
        if method == "workspace.events.wait":
            after, limit, timeout = req.get("after", 0), req.get("limit", 100), req.get("timeout", 15)
            if (type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500 or
                    type(timeout) not in (int, float) or not 0 <= timeout <= 20):
                raise ValueError("invalid event wait")
            deadline = time.monotonic() + timeout
            next_status = 0.0
            while True:
                result = self.dispatch({"method": "workspace.events", "workspace": name,
                                        "after": after, "limit": limit})
                if result["events"]:
                    return result
                if not self.db.execute("SELECT 1 FROM workspaces WHERE name=?", (name,)).fetchone():
                    raise ValueError("workspace does not exist")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result
                if time.monotonic() >= next_status:
                    # A failed zmx listing is an error, never a disappearance.
                    # Poll only after delivering already journaled events.
                    try:
                        self.dispatch({"method": "workspace.status", "workspace": name})
                    except Conflict:
                        # A concurrent committed layout invalidates only this
                        # status sample; fetch the journal again immediately.
                        continue
                    next_status = time.monotonic() + 1
                time.sleep(min(0.25, remaining))
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
                if self.db.execute("SELECT 1 FROM workspace_kills WHERE workspace=?", (name,)).fetchone():
                    raise Conflict("workspace termination is pending")
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
                if pane in self.read_inventory()["sessions"]:
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
                    if req.get("host_metrics") is True:
                        current["host_metrics"] = HOST_METRICS.get()
                    return current
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        if method == "workspace.status":
            row = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError("workspace does not exist")
            panes = set(json.loads(row[1]))
            inventory = self.read_inventory()
            live = panes.intersection(inventory["sessions"])
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
                        self.add_event(name, row[0], "session.live" if present else "session.missing",
                                       {"pane": pane, "inventory_generation": inventory["generation"],
                                        "observed_at": inventory["observed_at"], "source": inventory["source"],
                                        "listed_sessions": inventory["sessions"]})
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
        if method not in ("workspace.put", "workspace.recover-stage", "panes.close", "panes.exited", "workspace.prune", "workspace.reconcile"):
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
        cancellations = req.get("cancel_reservations", [])
        if (not isinstance(cancellations, list) or any(not isinstance(p, str) or not PANE.fullmatch(p) for p in cancellations) or
                len(cancellations) != len(set(cancellations)) or (cancellations and method != "workspace.reconcile")):
            raise ValueError("invalid reservation cancellation")
        if method in ("workspace.prune", "workspace.reconcile") and (removals or cancellations):
            raise ValueError("automatic absence-based reconciliation is disabled; workspace state retained")
        layout_sessions(snapshot, panes)
        if method == "workspace.reconcile" and not isinstance(req.get("lease_token"), str):
            raise ValueError("workspace reconciliation requires a lease")
        if method == "panes.exited" and (not isinstance(req.get("lease_token"), str) or
                                        not isinstance(req.get("exit_receipts"), dict) or
                                        set(req["exit_receipts"]) != set(removals)):
            raise ValueError("exit removal requires a lease and exact origin receipts")
        # The same durable intent may be retried by a newly fenced lease owner.
        # Authorization is checked separately before looking up the operation.
        intent = {key: value for key, value in req.items() if key != "lease_token"}
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.check_lease(name, req.get("lease_token"))
            if self.db.execute("SELECT 1 FROM workspace_kills WHERE workspace=?", (name,)).fetchone():
                raise Conflict("workspace termination is pending")
            previous = self.db.execute("SELECT digest,response FROM operations WHERE workspace=? AND id=?", (name, request_id)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise Conflict("request ID already used for a different operation")
                self.db.execute("COMMIT")
                return json.loads(previous[1])
            row = self.db.execute("SELECT revision,panes FROM workspaces WHERE name=?", (name,)).fetchone()
            retired = self.db.execute("SELECT revision FROM retired_workspaces WHERE name=?", (name,)).fetchone()
            actual, old = (row[0], set(json.loads(row[1]))) if row else (retired[0] if retired else 0, set())
            if revision != actual:
                raise Conflict(f"revision mismatch: expected {revision}, current {actual}")
            if row is None and retired and (method != "workspace.put" or panes or snapshot != "# kzw-dormant-workspace\n"):
                raise Conflict("retired workspace must be explicitly recreated")
            current, removed = set(panes), set(removals)
            if method in ("workspace.put", "workspace.recover-stage"):
                if removed or not old.issubset(current):
                    raise ValueError("workspace.put cannot remove panes")
            elif (not removed and not (method == "workspace.reconcile" and cancellations)) or removed != old - current or current - old:
                raise ValueError("close/prune must remove exactly the specified existing panes")
            if method == "panes.exited":
                if any(self.db.execute("SELECT 1 FROM " + table + " WHERE workspace=?", (name,)).fetchone()
                       for table in ("reservations", "staged_layouts", "cleanup")):
                    raise Conflict("unfinished workspace operation blocks exit removal")
                if self.exit_receipts(removed) != req["exit_receipts"]:
                    raise Conflict("session exit evidence is missing or changed; layout retained")
                self.check_lease(name, req["lease_token"])
            if method == "workspace.reconcile":
                reservations = {r[0] for r in self.db.execute("SELECT pane FROM reservations WHERE workspace=?", (name,))}
                if reservations != set(cancellations) or self.db.execute("SELECT 1 FROM staged_layouts WHERE workspace=?", (name,)).fetchone():
                    raise Conflict("reservations or staged layout changed before reconciliation")
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
                inventory = self.read_inventory()
                required = current if method == "workspace.recover-stage" else added
                if not required.issubset(inventory["sessions"]):
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
            if row is None and retired:
                self.db.execute("DELETE FROM retired_workspaces WHERE name=?", (name,))
            for pane in added:
                self.db.execute("INSERT OR IGNORE INTO observed_sessions VALUES (?,?)", (name, pane))
                self.db.execute("INSERT OR REPLACE INTO session_presence VALUES (?,?,1)", (name, pane))
                self.db.execute("DELETE FROM reservations WHERE workspace=? AND pane=?", (name, pane))
            for pane in removed:
                self.db.execute("DELETE FROM observed_sessions WHERE workspace=? AND pane=?", (name, pane))
                self.db.execute("DELETE FROM session_presence WHERE workspace=? AND pane=?", (name, pane))
            for pane in cancellations:
                self.db.execute("DELETE FROM reservations WHERE workspace=? AND pane=?", (name, pane))
            staged = self.db.execute("SELECT snapshot,panes FROM staged_layouts WHERE workspace=?", (name,)).fetchone()
            if staged and staged[0] == snapshot and set(json.loads(staged[1])) == current:
                self.db.execute("DELETE FROM staged_layouts WHERE workspace=?", (name,))
            reason = {"panes.close": "managed_close", "panes.exited": "confirmed_exit", "workspace.prune": "confirmed_absence",
                      "workspace.reconcile": "confirmed_absence",
                      "workspace.recover-stage": "stage_recovery"}.get(method, "snapshot")
            self.add_event(name, new_revision, "layout.updated", {"panes": sorted(current),
                                                                  "added": sorted(added), "removed": sorted(removed),
                                                                  "pending_cleanup": sorted(removed) if method == "panes.close" else [],
                                                                  "reason": reason,
                                                                  **({"exit_receipts": req["exit_receipts"]} if method == "panes.exited" else {}),
                                                                  **({"cancelled_reservations": sorted(cancellations)} if cancellations else {})})
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

    def rename_workspace(self, name, req):
        """Rename a detached workspace in one revision-fenced transaction."""
        destination, request_id, revision = req.get("destination"), req.get("request_id"), req.get("revision")
        if not isinstance(destination, str) or not NAME.fullmatch(destination) or destination == name:
            raise ValueError("invalid destination workspace name")
        if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id) or type(revision) is not int or revision < 0:
            raise ValueError("invalid workspace rename request")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute("SELECT destination,revision,result FROM workspace_renames WHERE workspace=? AND id=?",
                                       (name, request_id)).fetchone()
            if previous:
                if previous[:2] != (destination, revision):
                    raise Conflict("rename request ID already used for a different operation")
                self.db.execute("COMMIT")
                return json.loads(previous[2])
            row = self.db.execute("SELECT revision,snapshot,panes FROM workspaces WHERE name=?", (name,)).fetchone()
            if row is None or row[0] != revision:
                raise Conflict("workspace changed before rename")
            if self.db.execute("SELECT 1 FROM workspaces WHERE name=?", (destination,)).fetchone():
                raise Conflict("destination workspace already exists")
            for workspace in (name, destination):
                lease = self.db.execute("SELECT expires FROM leases WHERE workspace=?", (workspace,)).fetchone()
                if lease and lease[0] > time.time():
                    raise Conflict("workspace is attached; detach before renaming")
                for table in ("reservations", "staged_layouts", "cleanup", "workspace_kills"):
                    if self.db.execute(f"SELECT 1 FROM {table} WHERE workspace=?", (workspace,)).fetchone():
                        raise Conflict("workspace has unfinished operations")
            old_retired = self.db.execute("SELECT revision FROM retired_workspaces WHERE name=?", (name,)).fetchone()
            new_retired = self.db.execute("SELECT revision FROM retired_workspaces WHERE name=?", (destination,)).fetchone()
            old_revision = max(revision, old_retired[0] if old_retired else 0) + 1
            new_revision = max(old_revision, new_retired[0] if new_retired else 0) + 1
            panes = json.loads(row[2])
            self.db.execute("DELETE FROM workspaces WHERE name=?", (name,))
            self.db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", (destination, new_revision, row[1], row[2]))
            for table in ("observed_sessions", "session_presence"):
                self.db.execute(f"UPDATE {table} SET workspace=? WHERE workspace=?", (destination, name))
            self.db.execute("DELETE FROM leases WHERE workspace IN (?,?)", (name, destination))
            self.db.execute("INSERT OR REPLACE INTO retired_workspaces VALUES (?,?)", (name, old_revision))
            self.db.execute("DELETE FROM retired_workspaces WHERE name=?", (destination,))
            self.add_event(name, old_revision, "workspace.renamed", {"from": name, "to": destination, "panes": panes})
            self.add_event(destination, new_revision, "workspace.renamed", {"from": name, "to": destination, "panes": panes})
            result = {"name": destination, "revision": new_revision, "panes": panes}
            self.db.execute("INSERT INTO workspace_renames VALUES (?,?,?,?,?)",
                            (name, request_id, destination, revision, json.dumps(result)))
            self.db.execute("COMMIT")
            return result
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def retry_cleanup(self, name, token=None, unattended=False):
        # Serialize listing, kill, and acknowledgement. The background retry
        # is limited to already-authorized cleanup with no active lease.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if unattended:
                lease = self.db.execute("SELECT expires FROM leases WHERE workspace=?", (name,)).fetchone()
                if lease and lease[0] > time.time():
                    raise Conflict("workspace still has an active lease")
            else:
                self.check_lease(name, token)
            row = self.db.execute("SELECT revision FROM workspaces WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError("workspace does not exist")
            pending = [r[0] for r in self.db.execute("SELECT pane FROM cleanup WHERE workspace=?", (name,))]
            env = self.zmx_environment()
            live = set(self.read_inventory()["sessions"])
            failed = []
            for pane in pending:
                if pane in live:
                    result = subprocess.run([self.zmx, "kill", pane], capture_output=True, text=True, env=env, timeout=10)
                    if result.returncode:
                        failed.append(pane)
                        continue
                    if pane in self.read_inventory()["sessions"]:
                        failed.append(pane)
                        continue
                self.db.execute("DELETE FROM cleanup WHERE workspace=? AND pane=?", (name, pane))
                self.add_event(name, row[0], "pane.cleanup_completed", {"pane": pane, "reason": "managed_close"})
            self.db.execute("COMMIT")
            return {"remaining": failed}
        except Exception:
            self.db.execute("ROLLBACK")
            raise


def state_root():
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kzw"
    return root / "coordinator" if os.environ.get("KZW_LOCAL_COORDINATOR") == "1" else root


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

        def observe():
            try:
                store = Store(root / "remote.sqlite3")
                try:
                    while not stopping.is_set():
                        store.observe_detached()
                        stopping.wait(2)
                finally:
                    store.close()
            except (OSError, sqlite3.Error):
                # A later server restart can retry; never infer absence here.
                pass

        threading.Thread(target=observe, name="kzw-observer", daemon=True).start()

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
