"""Advisory connection health and private, identity-scoped retry endpoints.

These endpoints never mutate workspace metadata or signal a remote process.
Only the owner of an already affected local transport can cancel that transport.
"""

import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import uuid


class RetryServer:
    def __init__(self, workspace, origin, callback, pane="", directory=""):
        root = Path.home() / ".cache"
        root.mkdir(exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="kzw-retry-", dir=root))
        self.path = self.directory / "socket"
        self.identity = {"path": str(self.path), "token": uuid.uuid4().hex,
                         "workspace": workspace, "origin": origin, "pane": pane, "directory": str(directory)}
        self.callback = callback
        self.stop = threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server.bind(str(self.path))
            self.server.listen(16)
            self.server.settimeout(0.2)
        except BaseException:
            self.server.close()
            if self.path.exists():
                self.path.unlink()
            self.directory.rmdir()
            raise
        self.thread = threading.Thread(target=self.serve, name="kzw-retry", daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(0.2)
                try:
                    with client.makefile("rb") as stream:
                        raw = stream.readline(4097)
                    if len(raw) <= 4096 and raw.endswith(b"\n") and json.loads(raw) == self.identity:
                        self.callback()
                except (OSError, ValueError):
                    pass

    def close(self):
        self.stop.set()
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # Fall back to the accept timeout on platforms that reject this.
        self.thread.join(timeout=1)
        self.server.close()
        if self.thread.is_alive():
            raise OSError("retry endpoint did not stop")
        self.path.unlink()
        self.directory.rmdir()


def request_retry(identity, workspace, origin, pane="", directory=""):
    """Never infer a PID from terminal metadata, or execute a supplied command."""
    if (not isinstance(identity, dict) or identity.get("workspace") != workspace or
            identity.get("origin") != origin or identity.get("pane") != pane or
            identity.get("directory") != str(directory) or
            not isinstance(identity.get("path"), str) or not isinstance(identity.get("token"), str)):
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.1)
        client.connect(identity["path"])
        client.sendall(json.dumps(identity, separators=(",", ":")).encode() + b"\n")
    return True


class Health:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.sources = {}
        self.last_retry = float("-inf")
        self.network = None
        self.unsubscribe = lambda: None

    def bind_network(self, gate):
        with self.lock:
            if self.network is gate:
                return
            self.unsubscribe()
            self.network = gate
            self.unsubscribe = gate.subscribe(self.network_changed) if gate is not None else lambda: None

    def network_changed(self, previous, available):
        if previous is False and available is not False:
            self.retry(force=True)
            with self.lock:
                for source in self.sources.values():
                    source["wake"].set()

    def begin(self, name, channel, timeout, wake, idle_after=None):
        with self.lock:
            previous = self.sources.get(name, {})
            self.sources[name] = {"state": previous.get("state", "restoring"),
                                  "deadline": self.clock() + timeout, "channel": channel,
                                  "wake": wake, "idle_after": idle_after}

    def success(self, name):
        with self.lock:
            source = self.sources[name]
            source["state"] = "ok"
            idle = source["idle_after"]
            source["deadline"] = self.clock() + idle if idle else None
            source["channel"] = None

    def failed(self, name):
        with self.lock:
            source = self.sources[name]
            source.update(state="reconnecting", deadline=None, channel=None)

    def state(self):
        with self.lock:
            if self.network is not None and self.network.offline:
                return "offline"
            if any(s["deadline"] is not None and self.clock() >= s["deadline"] for s in self.sources.values()):
                return "stalled"
            states = {s["state"] for s in self.sources.values()}
            if "reconnecting" in states:
                return "reconnecting"
            return "restoring" if "restoring" in states else ""

    def retry(self, force=False):
        with self.lock:
            if self.network is not None and self.network.offline:
                return
            now = self.clock()
            if not force and now - self.last_retry < 1:
                return
            self.last_retry = now
            for source in self.sources.values():
                overdue = source["deadline"] is not None and now >= source["deadline"]
                if not overdue and (source["state"] != "reconnecting" or source["channel"] is not None):
                    continue
                source.update(state="reconnecting", deadline=None)
                source["wake"].set()
                channel = source["channel"]
                if channel is not None and channel.process.poll() is None:
                    try:
                        channel.process.terminate()
                    except ProcessLookupError:
                        pass


_workspaces = {}
_lock = threading.Lock()


def for_directory(directory):
    with _lock:
        return _workspaces.setdefault(str(directory), Health())


def forget(directory):
    with _lock:
        health = _workspaces.pop(str(directory), None)
    if health is not None:
        health.unsubscribe()


class Display:
    """Keep local redraws and retries independent of a blocked supervisor RPC."""

    def __init__(self, directory, workspace, origin):
        self.directory, self.workspace, self.origin = directory, workspace, origin
        self.health = for_directory(directory)
        self.endpoint = RetryServer(workspace, origin, self.health.retry, directory=directory)
        self.sample = (None, time.monotonic())
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="kzw-connection-display", daemon=True)
        self.thread.start()

    def metrics(self, sample):
        self.sample = (sample, time.monotonic())

    def run(self):
        import subprocess
        from .metrics import publish
        while not self.stop.is_set():
            sample, received = self.sample
            if isinstance(sample, dict):
                sample = dict(sample)
                age = sample.get("age_seconds")
                if type(age) in (int, float):
                    sample["age_seconds"] = age + time.monotonic() - received
            try:
                publish(self.directory, self.workspace, self.origin, sample,
                        connection=self.health.state(), retry=self.endpoint.identity)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        self.endpoint.close()
        self.thread.join(timeout=11)
        if self.thread.is_alive():
            raise OSError("connection display did not stop")
