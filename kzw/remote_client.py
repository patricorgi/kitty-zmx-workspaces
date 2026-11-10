"""Experimental single-writer remote workspace snapshots over OpenSSH."""

import base64
import hashlib
import json
import os
import re
import select
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import PANE, atomic_write, check_name

DORMANT = "# kzw-dormant-workspace\n"
ORIGIN = "KZW_ORIGIN"
EXECUTABLE = "KZW_PANE_EXECUTABLE"
MAX_MESSAGE = 1024 * 1024
_local = threading.local()
_channel_lock = threading.RLock()


def origin_state_home(host):
    """The remote test override must not affect local coordinator identity."""
    return os.environ.get("KZW_REMOTE_STATE_HOME", "") if host else ""


class RemoteRejected(ValueError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def ssh_command(host):
    if not isinstance(host, str) or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host):
        raise ValueError("invalid SSH host")
    # Ship our stdlib-only RPC handler over SSH; no remote install or TCP
    # listener is required. Remote state is persistent in its XDG state dir.
    source = Path(__file__).with_name("remote.py").read_bytes()
    program = ("import base64,os;os.environ.pop('KZW_LOCAL_COORDINATOR',None);os.environ['KZW_COORDINATOR_SOURCE']=%r;"
               "exec(base64.b64decode(os.environ['KZW_COORDINATOR_SOURCE']))") % base64.b64encode(source).decode()
    state_override = os.environ.get("KZW_REMOTE_STATE_HOME", "")
    if state_override and (not state_override.startswith("/") or "\x00" in state_override):
        raise ValueError("KZW_REMOTE_STATE_HOME must be an absolute remote path")
    command = ("XDG_STATE_HOME=" + shlex.quote(state_override) + " " if state_override else "") + "python3 -u -c " + shlex.quote(program)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command]


def coordinator_command(host):
    """Use the same protocol locally or over the SSH bridge."""
    if host:
        return ssh_command(host)
    program = ("import os;os.environ.pop('KZW_COORDINATOR_SOURCE',None);"
               "os.environ['KZW_LOCAL_COORDINATOR']='1';from kzw.remote import main;main()")
    return [sys.executable, "-u", "-c", program]


class Channel:
    """One ordered RPC stream; a broken stream is never silently replayed."""

    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.buffer = b""

    def request(self, request, timeout=30):
        data = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_MESSAGE:
            raise ValueError("remote request too large")
        deadline = time.monotonic() + timeout
        offset = 0
        while offset < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                raise TimeoutError("remote coordinator request timed out")
            offset += os.write(self.process.stdin.fileno(), data[offset:])
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("remote coordinator response timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise OSError("remote coordinator disconnected; operation outcome may be unknown")
            self.buffer += chunk
            if len(self.buffer) > MAX_MESSAGE:
                raise ValueError("remote coordinator response too large")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return decode_response(line)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()


@contextmanager
def persistent_rpc():
    """Reuse a connection in this thread; one-shot CLI commands stay independent."""
    previous = getattr(_local, "channels", None)
    _local.channels = {}
    try:
        yield
    finally:
        with _channel_lock:
            for channel in _local.channels.values():
                channel.close()
        _local.channels = previous


def decode_response(data):
    try:
        response = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid remote coordinator response") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        if not isinstance(response, dict):
            raise ValueError("invalid remote coordinator response")
        raise RemoteRejected("remote coordinator: " + str(response.get("error", "unknown error")), response.get("code"))
    if "result" not in response:
        raise ValueError("invalid remote coordinator response")
    return response["result"]


def call(host, request):
    broker = os.environ.get("KZW_RPC_SOCKET")
    if (broker and host == os.environ.get("KZW_WORKSPACE_ORIGIN", "") and
            request.get("workspace") == os.environ.get("KZW_WORKSPACE") and request.get("workspace")):
        return broker_call(broker, host, request)
    return direct_call(host, request)


def direct_call(host, request):
    channels = getattr(_local, "channels", None)
    if channels is None:
        proc = subprocess.run(coordinator_command(host), input=json.dumps(request, separators=(",", ":")) + "\n",
                              text=True, capture_output=True, timeout=30, check=True)
        return decode_response(proc.stdout)
    key = (host, os.environ.get("KZW_REMOTE_STATE_HOME", "") if host else os.environ.get("XDG_STATE_HOME", ""))
    with _channel_lock:
        if key not in channels:
            channels[key] = Channel(coordinator_command(host))
        try:
            return channels[key].request(request)
        except RemoteRejected:
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            channels.pop(key).close()
            raise


def broker_call(path, host, request):
    message = json.dumps({"host": host, "request": request}, separators=(",", ":")).encode() + b"\n"
    if len(message) > MAX_MESSAGE:
        raise ValueError("coordinator request too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(35)
        client.connect(path)
        client.sendall(message)
        with client.makefile("rb") as stream:
            data = stream.readline(MAX_MESSAGE + 1)
    if not data or len(data) > MAX_MESSAGE or not data.endswith(b"\n"):
        raise OSError("coordinator broker disconnected; operation outcome may be unknown")
    response = json.loads(data)
    if response.get("transport_error"):
        raise OSError("coordinator broker: " + str(response["transport_error"]))
    return decode_response(data)


class ConnectionBroker:
    """Share the supervisor's ordered RPC stream with its local child processes."""

    def __init__(self, host, workspace):
        self.host = host
        self.workspace = check_name(workspace)
        self.channels = getattr(_local, "channels", None)
        if self.channels is None:
            raise ValueError("connection broker requires persistent RPC")
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="kzw-rpc-", dir=cache))
        self.path = self.directory / "socket"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server.bind(str(self.path))
            self.server.listen(16)
            self.server.settimeout(0.2)
        except Exception:
            self.server.close()
            if self.path.exists():
                self.path.unlink()
            self.directory.rmdir()
            raise
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        _local.channels = self.channels
        while not self.stopping.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(35)
                try:
                    with client.makefile("rb") as stream:
                        raw = stream.readline(MAX_MESSAGE + 1)
                    if not raw.endswith(b"\n") or len(raw) > MAX_MESSAGE:
                        raise ValueError("invalid broker request")
                    msg = json.loads(raw)
                    if (not isinstance(msg, dict) or msg.get("host") != self.host or
                            not isinstance(msg.get("request"), dict) or msg["request"].get("workspace") != self.workspace):
                        raise ValueError("broker origin mismatch")
                    result = direct_call(self.host, msg["request"])
                    response = {"ok": True, "result": result}
                except RemoteRejected as exc:
                    response = {"ok": False, "error": str(exc), "code": exc.code}
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    response = {"transport_error": str(exc)}
                try:
                    client.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                except OSError:
                    pass

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=37)
        if self.thread.is_alive():
            raise OSError("coordinator broker did not stop; socket retained")
        self.server.close()
        self.path.unlink()
        self.directory.rmdir()


def translate(snapshot, executable, host, portable):
    """Rewrite managed launch commands without replaying arbitrary programs."""
    if snapshot in (DORMANT, "# kzw-empty-workspace\n"):
        return snapshot, []
    lines, found = [], []
    for line in snapshot.splitlines():
        if line.startswith("cd "):
            # Local client paths are not portable; live shells retain cwd.
            continue
        if not line.startswith("launch "):
            lines.append(line)
            continue
        args = shlex.split(line)
        if len(args) < 7 or not args[1].startswith("kitty-unserialize-data="):
            raise ValueError("unsupported remote workspace launch")
        # Coordinator-generated commands end with executable --pane ID --host HOST --existing.
        command = args[-6:]
        if command[1] != "--pane" or command[3] != "--host" or command[5] != "--existing" or not PANE.fullmatch(command[2]):
            raise ValueError("remote workspace contains an unmanaged launch")
        if portable and command[4] != host:
            raise ValueError("remote workspace may only contain panes on its origin host")
        if not portable and (command[0] != EXECUTABLE or command[4] != ORIGIN):
            raise ValueError("remote workspace has invalid portable launch")
        args = args[:-6]
        args = [arg for arg in args if not arg.startswith("--cwd=") and not arg.startswith("--var=kzw_host=")]
        args.append("--var=kzw_host=" + (ORIGIN if portable else host))
        args.extend([EXECUTABLE if portable else executable, "--pane", command[2], "--host",
                     ORIGIN if portable else host, "--existing"])
        found.append(command[2])
        lines.append(shlex.join(args))
    if not found or len(found) != len(set(found)):
        raise ValueError("remote workspace has no managed panes or duplicate pane IDs")
    return "\n".join(lines) + "\n", sorted(found)


def snapshot_hash(snapshot, panes):
    payload = json.dumps([snapshot, sorted(panes)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def save_config(directory, config):
    atomic_write(directory / "remote.json", json.dumps(config, separators=(",", ":")) + "\n")


def lease_token(directory):
    path = directory / "lease.json"
    if not path.exists():
        return None
    lease = json.loads(path.read_text())
    token = lease.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid local workspace lease")
    return token


def reserve_pane(directory, pane, operation):
    if not PANE.fullmatch(pane):
        raise ValueError("invalid pane ID")
    config = json.loads((directory / "remote.json").read_text())
    if config.get("state_home", "") != origin_state_home(config["host"]):
        raise ValueError("remote state directory differs from this workspace's origin")
    if config.get("pending"):
        raise ValueError("an earlier remote update is pending")
    token = lease_token(directory)
    if token is None:
        raise ValueError("remote workspace has no active lease")
    return call(config["host"], {"method": "pane.reserve", "workspace": config["workspace"],
                                 "pane": pane, "request_id": operation, "revision": config["revision"],
                                 "lease_token": token})


def stage_layout(directory, snapshot, panes):
    config = json.loads((directory / "remote.json").read_text())
    if config.get("state_home", "") != origin_state_home(config["host"]):
        raise ValueError("remote state directory differs from this workspace's origin")
    if config.get("pending"):
        raise ValueError("an earlier remote update is pending")
    token = lease_token(directory)
    if token is None:
        raise ValueError("remote workspace has no active lease")
    portable, parsed = translate(snapshot, "", config["host"], True)
    if sorted(panes) != parsed:
        raise ValueError("staged layout and pane manifest disagree")
    return call(config["host"], {"method": "workspace.stage", "workspace": config["workspace"],
                                 "revision": config["revision"], "snapshot": portable, "panes": parsed,
                                 "lease_token": token})


def acquire_lease(directory, name, host):
    path = directory / "lease.json"
    config_path = directory / "remote.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        if (config.get("host") != host or config.get("workspace") != name or
                config.get("state_home", "") != origin_state_home(host)):
            raise ValueError("local workspace has a conflicting remote origin")
    if not call(host, {"method": "workspace.get", "workspace": name})["exists"]:
        raise ValueError("remote workspace does not exist")
    if path.exists():
        record = json.loads(path.read_text())
        if (record.get("host") != host or record.get("workspace") != name or
                record.get("state_home", "") != origin_state_home(host)):
            raise ValueError("local workspace lease has a conflicting origin")
        token = lease_token(directory)
        state = call(host, {"method": "workspace.lease.inspect", "workspace": name, "token": token})["state"]
        if state in ("own-expired", "other-expired"):
            token = uuid.uuid4().hex
            atomic_write(path, json.dumps({"host": host, "workspace": name, "token": token,
                                           "state_home": origin_state_home(host)}) + "\n")
    else:
        token = uuid.uuid4().hex
        atomic_write(path, json.dumps({"host": host, "workspace": name, "token": token,
                                       "state_home": origin_state_home(host)}) + "\n")
    call(host, {"method": "workspace.lease.acquire", "workspace": name, "token": token})
    return token


def renew_lease(directory):
    token = lease_token(directory)
    if token is None:
        raise ValueError("remote workspace has no active lease")
    lease = json.loads((directory / "lease.json").read_text())
    if lease.get("state_home", "") != origin_state_home(lease["host"]):
        raise ValueError("remote state directory differs from this workspace's origin")
    call(lease["host"], {"method": "workspace.lease.renew", "workspace": lease["workspace"], "token": token})


def release_lease(directory):
    path = directory / "lease.json"
    if not path.exists():
        return
    lease = json.loads(path.read_text())
    if lease.get("state_home", "") != origin_state_home(lease["host"]):
        raise ValueError("remote state directory differs from this workspace's origin")
    token = lease_token(directory)
    call(lease["host"], {"method": "workspace.lease.release", "workspace": lease["workspace"], "token": token})
    if path.exists() and lease_token(directory) == token:
        path.unlink()


def start_lease_heartbeat(directory):
    lease = json.loads((directory / "lease.json").read_text())
    token = lease_token(directory)
    stop = threading.Event()

    def heartbeat():
        failed = False
        with persistent_rpc():
            while not stop.wait(5):
                try:
                    call(lease["host"], {"method": "workspace.lease.renew", "workspace": lease["workspace"], "token": token})
                    if failed:
                        print("kzw: workspace lease renewed", file=sys.stderr, flush=True)
                    failed = False
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    if not failed:
                        print(f"kzw: workspace lease renewal failed; remote mutations are fenced: {exc}",
                              file=sys.stderr, flush=True)
                    failed = True

    thread = threading.Thread(target=heartbeat, name="kzw-lease", daemon=True)
    thread.start()

    def close():
        stop.set()
        thread.join(timeout=1)

    return close


def publish(directory, snapshot, panes, confirmed_exits=False, confirmed_close=False, close_panes=None):
    """Publish with durable replay of requests whose response was lost."""
    config_path = directory / "remote.json"
    if not config_path.exists():
        return
    config = json.loads(config_path.read_text())
    host, name = config["host"], check_name(config["workspace"])
    if config.get("state_home", "") != origin_state_home(host):
        raise ValueError("remote state directory differs from this workspace's origin")
    portable, parsed = translate(snapshot, "", host, True)
    if sorted(panes) != parsed:
        raise ValueError("snapshot and remote pane manifest disagree")
    desired = snapshot_hash(portable, parsed)
    while True:
        pending = config.get("pending")
        if pending:
            if confirmed_close and (pending["request"]["method"] != "panes.close" or
                                    set(pending["request"].get("remove", [])) != set(close_panes or [])):
                raise ValueError("pending remote operation differs from confirmed close")
            current_token = lease_token(directory)
            if current_token is not None and pending["request"].get("lease_token") != current_token:
                pending["request"]["lease_token"] = current_token
                save_config(directory, config)
            try:
                response = call(host, pending["request"])
            except RemoteRejected as exc:
                # A typed rejection proves this particular registration was
                # rolled back. Unknown outcomes retain the durable request.
                if exc.code == "session_not_ready" and pending["request"]["method"] == "workspace.put":
                    config.pop("pending", None)
                    save_config(directory, config)
                raise
            config["revision"] = response["revision"]
            config["published_hash"] = pending["hash"]
            config["panes"] = pending["request"]["panes"]
            config.pop("pending", None)
            save_config(directory, config)
        if config.get("published_hash") == desired:
            return
        old_panes = set(config.get("panes", []))
        removed = old_panes - set(parsed)
        if confirmed_close and removed != set(close_panes or []):
            raise ValueError("remote pane removal differs from confirmed close")
        if removed and not (confirmed_exits or confirmed_close):
            raise ValueError("remote pane removal requires a confirmed shell exit")
        method = ("panes.close" if confirmed_close else "workspace.prune") if removed else "workspace.put"
        request = {"method": method, "workspace": name, "revision": config["revision"],
                   "request_id": uuid.uuid4().hex, "snapshot": portable, "panes": parsed}
        token = lease_token(directory)
        if token is not None:
            request["lease_token"] = token
        if removed:
            request["remove"] = sorted(removed)
        config["pending"] = {"request": request, "hash": desired}
        save_config(directory, config)


def retry_cleanup(directory):
    config = json.loads((directory / "remote.json").read_text())
    if config.get("state_home", "") != origin_state_home(config["host"]):
        raise ValueError("remote state directory differs from this workspace's origin")
    request = {"method": "cleanup.retry", "workspace": config["workspace"]}
    token = lease_token(directory)
    if token is not None:
        request["lease_token"] = token
    result = call(config["host"], request)
    if result["remaining"]:
        raise ValueError("remote session cleanup incomplete: " + ", ".join(result["remaining"]))


def status(directory):
    """Ask the origin about owned sessions; transport failures mean unknown."""
    config = json.loads((directory / "remote.json").read_text())
    if config.get("state_home", "") != origin_state_home(config["host"]):
        raise ValueError("remote state directory differs from this workspace's origin")
    return call(config["host"], {"method": "workspace.status", "workspace": config["workspace"]})


def ensure_current_revision(directory):
    """Refuse view mutations while remote state differs or an update is unknown."""
    config = json.loads((directory / "remote.json").read_text())
    if config.get("state_home", "") != origin_state_home(config["host"]):
        raise ValueError("remote state directory differs from this workspace's origin")
    if config.get("pending"):
        raise ValueError("remote update outcome is pending; retry before changing the view")
    remote = call(config["host"], {"method": "workspace.get", "workspace": config["workspace"]})
    if not remote.get("exists") or remote.get("revision") != config.get("revision"):
        raise ValueError("remote workspace revision changed; reconnect before changing the view")


def start_status_monitor(directory):
    """Wake the supervisor when the origin reports an observed session missing."""
    config = json.loads((directory / "remote.json").read_text())
    if config.get("state_home", "") != origin_state_home(config["host"]):
        raise ValueError("remote state directory differs from this workspace's origin")
    host, name = config["host"], check_name(config["workspace"])
    cursor = config.get("event_cursor", 0)
    if type(cursor) is not int or cursor < 0:
        raise ValueError("invalid saved workspace event cursor")
    stop = threading.Event()
    lock = threading.Lock()
    active = [None]

    def monitor():
        nonlocal cursor
        while not stop.is_set():
            channel = None
            try:
                channel = Channel(coordinator_command(host))
                with lock:
                    active[0] = channel
                    if stop.is_set():
                        continue
                known, notified = None, None
                while not stop.is_set():
                    current = channel.request({"method": "workspace.watch", "workspace": name,
                                               "known": known, "timeout": 2}, timeout=15)
                    if (not isinstance(current, dict) or type(current.get("revision")) is not int or
                            not isinstance(current.get("live"), list) or not isinstance(current.get("missing"), list) or
                            not isinstance(current.get("confirmed_missing"), list) or
                            any(not isinstance(p, str) or not PANE.fullmatch(p)
                                for p in current["live"] + current["missing"] + current["confirmed_missing"]) or
                            not set(current["confirmed_missing"]).issubset(current["missing"])):
                        raise ValueError("invalid workspace status notification")
                    missing = sorted(set(current["confirmed_missing"]))
                    next_cursor = cursor
                    while not stop.is_set():
                        page = channel.request({"method": "workspace.events", "workspace": name,
                                                "after": next_cursor, "limit": 100}, timeout=15)
                        events = page.get("events") if isinstance(page, dict) else None
                        if not isinstance(events, list):
                            raise ValueError("invalid workspace event page")
                        for event in events:
                            if (not isinstance(event, dict) or type(event.get("seq")) is not int or
                                    event["seq"] <= next_cursor or type(event.get("revision")) is not int or
                                    not isinstance(event.get("kind"), str) or not isinstance(event.get("data"), dict)):
                                raise ValueError("invalid or unordered workspace event")
                            next_cursor = event["seq"]
                        if len(events) < 100:
                            break
                    notice = (current["revision"], tuple(missing), next_cursor)
                    if notice != notified:
                        atomic_write(directory / "remote-status.changed",
                                     json.dumps({"missing": missing, "revision": current["revision"],
                                                 "event_cursor": next_cursor, "id": uuid.uuid4().hex}) + "\n")
                        notified = notice
                    cursor = next_cursor
                    known = current
            except (OSError, ValueError, subprocess.SubprocessError):
                if not stop.is_set():
                    stop.wait(1)
            finally:
                with lock:
                    active[0] = None
                if channel is not None:
                    channel.close()

    thread = threading.Thread(target=monitor, name="kzw-remote-status", daemon=True)
    thread.start()

    def close():
        stop.set()
        with lock:
            channel = active[0]
            if channel is not None and channel.process.poll() is None:
                channel.process.terminate()
        thread.join(timeout=3)

    return close


def inert_legacy_artifacts(directory, ignored_names=()):
    """Accept only known empty remnants, never unknown or recoverable state.

    The caller holds the workspace lock while checking and creating state.
    Do not follow symlinks or remove any of these files.
    """
    for path in directory.iterdir():
        if path.name in ignored_names:
            continue
        info = path.lstat()
        if path.name in ("lock", "capture.lock", "watcher.changed"):
            if not stat.S_ISREG(info.st_mode) or info.st_size:
                return False
        elif path.name == "native-pending":
            if not stat.S_ISDIR(info.st_mode) or any(path.iterdir()):
                return False
        elif re.fullmatch(r"\.retired-[0-9a-f]{32}", path.name):
            if not stat.S_ISDIR(info.st_mode):
                return False
        else:
            return False
    return True


def create(directory, name, host):
    check_name(name)
    config_path = directory / "remote.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        if (config.get("workspace") != name or config.get("host") != host
                or config.get("state_home", "") != origin_state_home(host)):
            raise ValueError("local workspace already exists; refusing to overwrite it")
        if not config.get("creating"):
            if config.get("pending"):
                raise ValueError("local workspace has a pending operation; refusing to overwrite it")
            remote = call(host, {"method": "workspace.get", "workspace": name})
            if remote["exists"] or remote.get("revision", 0) <= config.get("revision", 0):
                raise ValueError("local workspace already exists; refusing to overwrite it")
            allowed = {"lock", "capture.lock", "watcher.changed", "remote-status.changed", "remote.json",
                       "lease.json", "workspace.kitty-session", "panes.json", "bootstrap.kitty-session",
                       "native-pending"}
            for path in directory.iterdir():
                if path.name in ("pending-create.json", "pending-close.json", "native-stage.json", "kitty.sock"):
                    raise ValueError("local workspace has unfinished operations; refusing to overwrite it")
                if path.name not in allowed and not re.fullmatch(r"\.retired-[0-9a-f]{32}", path.name):
                    raise ValueError("local workspace contains unknown state; refusing to overwrite it")
                if path.name == "native-pending" and (path.is_symlink() or not path.is_dir() or any(path.iterdir())):
                    raise ValueError("local workspace has unfinished operations; refusing to overwrite it")
            archive = directory / (".retired-" + uuid.uuid4().hex)
            archive.mkdir(mode=0o700)
            # Move config last; a crash cannot make old metadata look like a
            # freshly initialized cache. Archives are retained for inspection.
            for path in list(directory.iterdir()):
                if path.name in allowed and path.name not in ("lock", "remote.json"):
                    path.rename(archive / path.name)
            config_path.rename(archive / "remote.json")
            config = {"workspace": name, "host": host, "revision": remote["revision"], "creating": True,
                      "state_home": origin_state_home(host)}
            save_config(directory, config)
    else:
        if not inert_legacy_artifacts(directory):
            raise ValueError("local workspace already exists; refusing to overwrite it")
        remote = call(host, {"method": "workspace.get", "workspace": name})
        if remote["exists"]:
            raise ValueError("remote workspace already exists; use attach instead")
        config = {"workspace": name, "host": host, "revision": remote.get("revision", 0), "creating": True,
                  "state_home": origin_state_home(host)}
        save_config(directory, config)
    publish(directory, DORMANT, [])
    config = json.loads(config_path.read_text())
    config.pop("creating", None)
    save_config(directory, config)


def pull(directory, name, host, executable):
    check_name(name)
    path = directory / "remote.json"
    if path.exists():
        config = json.loads(path.read_text())
        if (config.get("host") != host or config.get("workspace") != name or config.get("pending") or config.get("creating")
                or config.get("state_home", "") != origin_state_home(host)):
            raise ValueError("local workspace has conflicting or pending remote state")
    elif any(directory.iterdir()):
        # The workspace lock is created by the caller before this check.
        if any(p.name not in ("lock", "lease.json") for p in directory.iterdir()):
            raise ValueError("local workspace already exists; refusing to overwrite it")
    remote = call(host, {"method": "workspace.sync", "workspace": name})
    if not remote["exists"]:
        raise ValueError("remote workspace does not exist")
    staged = remote.get("staged")
    if staged and remote.get("reservations"):
        _, staged_panes = translate(staged["snapshot"], executable, host, False)
        if staged_panes != sorted(staged["panes"]) or staged["revision"] != remote["revision"]:
            raise ValueError("remote staged layout is inconsistent")
        token = lease_token(directory)
        if token is None:
            raise ValueError("remote workspace has no active lease")
        digest = snapshot_hash(staged["snapshot"], staged_panes)
        call(host, {"method": "workspace.recover-stage", "workspace": name,
                    "revision": staged["revision"], "snapshot": staged["snapshot"], "panes": staged_panes,
                    "request_id": "recover-" + digest, "lease_token": token})
        remote = call(host, {"method": "workspace.sync", "workspace": name})
    if remote.get("reservations"):
        raise ValueError("remote workspace has an incomplete pane creation; inspect its reservations before attaching")
    snapshot, panes = translate(remote["snapshot"], executable, host, False)
    if panes != sorted(remote["panes"]):
        raise ValueError("remote snapshot and pane manifest disagree")
    # A dormant record has no pre-existing layout. A first attachment starts a
    # managed shell and publishes its snapshot once Kitty has created it.
    if snapshot != DORMANT:
        atomic_write(directory / "workspace.kitty-session", snapshot)
        atomic_write(directory / "panes.json", json.dumps(panes) + "\n")
    elif (directory / "workspace.kitty-session").exists():
        raise ValueError("dormant remote state conflicts with a local snapshot")
    save_config(directory, {"workspace": name, "host": host, "revision": remote["revision"], "panes": panes,
                            "state_home": origin_state_home(host),
                            "event_cursor": remote["event_cursor"],
                            "published_hash": snapshot_hash(remote["snapshot"], panes)})
