"""Control only this wrapper's affected SSH child; never its shared master."""

import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

from .connection import RetryServer
from . import network


class NotStarted(OSError):
    """This attempt never spawned the terminal SSH child (master setup may run)."""


def user_var(key, value):
    encoded = base64.b64encode(value.encode()).decode()
    sys.stdout.write(f"\x1b]1337;SetUserVar={key}={encoded}\x07")
    sys.stdout.flush()


def ready_frame(data, marker, acknowledged=False):
    if not acknowledged and marker in data:
        data = data.replace(marker, b"", 1)
        acknowledged = True
    keep = 0
    if not acknowledged:
        for size in range(1, min(len(data), len(marker) - 1) + 1):
            if data.endswith(marker[:size]):
                keep = size
    output, pending = (data[:-keep], data[-keep:]) if keep else (data, b"")
    return output, pending, acknowledged


def foreground_group(tty, group):
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        os.tcsetpgrp(tty, group)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class Attachment:
    def __init__(self, workspace, origin, pane, directory="", *, existing=False):
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.process = None
        self.process_group = None
        self.child_changed = threading.Event()
        self.transport_ended = False
        self.network = network.get()
        self.resumed = self.network.resumes
        self.offline_phase = None
        self.initial_phase = "restoring" if existing else "connecting"
        self.phase = self.initial_phase
        self.started = time.monotonic()
        self.direct = False
        self.preflights = []
        self.nonce = uuid.uuid4().hex
        self.marker = b"\x1b]kzw-ready;" + self.nonce.encode() + b"\x07"
        self.ready_command = "printf '\\033]kzw-ready;%s\\007' " + self.nonce + "; "
        self.endpoint = RetryServer(workspace, origin, self.retry, pane, directory)
        user_var("kzw_retry_endpoint", json.dumps(self.endpoint.identity, separators=(",", ":")))
        self.set_phase(self.initial_phase)
        self.thread = threading.Thread(target=self.monitor, name="kzw-attachment-health", daemon=True)
        self.thread.start()

    def set_phase(self, value):
        if value and value != "offline" and self.network.offline:
            self.offline_phase = value
            value = "offline"
        elif value != "offline":
            self.offline_phase = None
        self.phase = value
        user_var("kzw_connection", value)

    def monitor(self):
        while not self.stop.wait(0.2):
            with self.lock:
                if self.network.offline:
                    if self.phase and self.phase != "offline":
                        self.set_phase(self.phase)
                    continue
                if self.phase == "offline":
                    previous = self.offline_phase or self.initial_phase
                    self.set_phase(previous)
                    if self.process is None:
                        self.started = time.monotonic()
                    elif previous in ("stalled", "reconnecting") or time.monotonic() - self.started >= 8:
                        self.set_phase("stalled")
                        self.retry()
                if self.phase in ("connecting", "restoring") and time.monotonic() - self.started >= 8:
                    self.set_phase("stalled")

    def retry(self):
        with self.lock:
            if self.network.offline:
                return
            if self.phase not in ("stalled", "reconnecting") or self.wake.is_set():
                return
            self.direct = True
            self.wake.set()
            self.child_changed.set()
            self.set_phase("reconnecting")
            if self.process is not None and self.process.poll() is None:
                try:
                    self.signal_child(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def ended(self, *, show_reconnecting=True):
        with self.lock:
            if self.process is not None:
                self.initial_phase = "restoring"
            self.process = None
            self.process_group = None
            self.direct = True
            if show_reconnecting:
                self.set_phase("reconnecting")

    def signal_child(self, signum):
        if self.process_group is not None:
            os.killpg(self.process_group, signum)
        elif signum == signal.SIGTERM:
            self.process.terminate()
        else:
            self.process.kill()

    def run(self, command, env):
        self.transport_ended = False
        self.network.require(True)
        from .ssh_transport import kitten_command, prepare_attachment
        use_kitten = (not self.direct and env.get("KZW_SSH_KITTEN") == "1"
                      and env.get("KITTY_PID") and env.get("KITTY_WINDOW_ID")
                      and sys.version_info >= (3, 11) and sys.stdin.isatty())
        if self.direct:
            # Bypass a possibly stale master, without stopping or unlinking it.
            tail = command.index("-t")
            command = ["ssh", "-o", "ControlMaster=no", "-o", "ControlPersist=no",
                       "-o", "ControlPath=none", *command[tail:]]
        else:
            done = threading.Event()
            result = []

            def prepare():
                try:
                    result.append(prepare_attachment(command, env))
                except Exception as exc:
                    result.append(exc)
                finally:
                    done.set()

            preparation = threading.Thread(target=prepare, daemon=True)
            self.preflights.append(preparation)
            preparation.start()
            while not done.wait(0.1):
                if self.wake.is_set():
                    # Let command-free master setup finish on its own. It may
                    # already serve another pane: never kill that process.
                    raise NotStarted("attachment preparation interrupted for retry")
            if isinstance(result[0], Exception):
                raise NotStarted(str(result[0])) from result[0]
            command = result[0]
        # Keepalive messages belong to this existing transport, not a new poll.
        command = [command[0], "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", *command[1:]]
        tty, original_group = None, None
        if use_kitten:
            tty = sys.stdin.fileno()
            original_group = os.tcgetpgrp(tty)
            use_kitten = original_group == os.getpgrp()
            if use_kitten:
                command = kitten_command(command)
        with self.lock:
            self.network.require(True)
            if self.wake.is_set():
                raise NotStarted("attachment interrupted for retry")
            self.started = time.monotonic()
            self.set_phase(self.initial_phase)
            try:
                options = {"process_group": 0} if use_kitten else {}
                self.process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                **options)
            except OSError as exc:
                raise NotStarted(str(exc)) from exc
            process = self.process
            self.child_changed.clear()
            if use_kitten:
                self.process_group = process.pid
                try:
                    foreground_group(tty, process.pid)
                    # If it reached the terminal before the handoff, it may
                    # have been stopped by the kernel's job control.
                    os.killpg(process.pid, signal.SIGCONT)
                except OSError:
                    if process.poll() is None:
                        self.signal_child(signal.SIGKILL)
                        process.wait()
                    foreground_group(tty, original_group)
                    process.stdout.close()
                    process.stderr.close()
                    self.ended()
                    # A child was spawned; the caller must not replay startup.
                    raise

        def forward():
            from .pane import ssh_diagnostic_visible
            for raw in iter(process.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                if env.get("KZW_DEBUG") == "1" or ssh_diagnostic_visible(line):
                    print(line, end="", file=sys.stderr, flush=True)

        acknowledged = False

        def output():
            nonlocal acknowledged
            pending = b""
            while True:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                data, pending, ready = ready_frame(pending, self.marker, acknowledged)
                if ready and not acknowledged:
                    with self.lock:
                        if self.process is process and not self.wake.is_set():
                            self.set_phase("")
                acknowledged = ready
                with self.lock:
                    while data:
                        data = data[os.write(sys.stdout.fileno(), data):]
            with self.lock:
                while pending:
                    pending = pending[os.write(sys.stdout.fileno(), pending):]

        thread = threading.Thread(target=forward, daemon=True)
        output_thread = threading.Thread(target=output, daemon=True)
        thread.start()
        output_thread.start()

        def wait_for_exit():
            process.wait()
            self.child_changed.set()

        waiter = threading.Thread(target=wait_for_exit, daemon=True)
        waiter.start()
        try:
            self.child_changed.wait()
            if self.wake.is_set():
                # Escalate only the exact local SSH child if cancellation is ignored.
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.signal_child(signal.SIGKILL)
            return subprocess.CompletedProcess(command, process.wait())
        finally:
            if process.poll() is None:
                try:
                    self.signal_child(signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.signal_child(signal.SIGKILL)
                    process.wait()
            if use_kitten:
                try:
                    foreground_group(tty, original_group)
                except OSError:
                    pass  # Kitty can close the terminal before the child exits.
            thread.join(timeout=1)
            output_thread.join(timeout=1)
            waiter.join()
            self.transport_ended = acknowledged and not self.wake.is_set() and not self.stop.is_set()
            process.stderr.close()
            process.stdout.close()
            self.ended(show_reconnecting=not self.transport_ended)

    def close(self):
        self.stop.set()
        with self.lock:
            process = self.process
            if process is not None and process.poll() is None:
                try:
                    self.signal_child(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.endpoint.close()
        self.thread.join(timeout=1)
        for preparation in self.preflights:
            preparation.join(timeout=16)
