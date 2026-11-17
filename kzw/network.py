"""Local network notifications, not Internet probes or another SSH poll.

Known offline pauses new remote work. Unknown status remains permissive so an
unsupported or failed OS observer cannot permanently fence a reachable host.
"""

from contextlib import contextmanager
import ctypes
import socket
import sys
import threading
import time


class OfflineError(OSError):
    pass


class Gate:
    def __init__(self, available=None):
        self.condition = threading.Condition()
        self.available = available
        self.resumes = 0
        self.callbacks = set()

    @property
    def offline(self):
        with self.condition:
            return self.available is False

    def update(self, available):
        if available is not None and type(available) is not bool:
            raise ValueError("invalid network availability")
        with self.condition:
            previous = self.available
            if previous is available:
                return
            self.available = available
            if previous is False and available is not False:
                self.resumes += 1
            self.condition.notify_all()
            callbacks = tuple(self.callbacks)
        for callback in callbacks:
            callback(previous, available)

    def subscribe(self, callback):
        with self.condition:
            self.callbacks.add(callback)

        def unsubscribe():
            with self.condition:
                self.callbacks.discard(callback)

        return unsubscribe

    def require(self, host):
        if host and self.offline:
            raise OfflineError("network offline; connection attempts paused")

    def wait_online(self, stop=None):
        with self.condition:
            while self.available is False:
                if stop is not None and stop.is_set():
                    return False
                # Only shutdown is checked periodically. Network restoration
                # wakes this condition directly; no network query is performed.
                self.condition.wait(0.2)
            return stop is None or not stop.is_set()

    def wait_retry(self, seconds, wake, stop=None, since=None):
        deadline = time.monotonic() + seconds
        with self.condition:
            resumed = self.resumes if since is None else since
            paused = self.available is False
            while stop is None or not stop.is_set():
                if self.available is False:
                    paused = True
                    self.condition.wait(0.2)
                    continue
                remaining = deadline - time.monotonic()
                if paused or self.resumes != resumed or wake.is_set() or remaining <= 0:
                    return True
                self.condition.wait(min(0.2, remaining))
            return False


def usable(flags):
    reachable, required, traffic, intervention, demand = 2, 4, 8, 16, 32
    return bool(flags & reachable and
                (not flags & required or flags & (traffic | demand) and not flags & intervention))


class DarwinMonitor:
    """Observe IPv4/IPv6 default-route reachability on a private CF run loop."""

    def __init__(self, gate):
        self.gate = gate
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="kzw-network", daemon=True)
        self.run_loop = None
        self.cf = None
        self.error = None

    def start(self):
        self.thread.start()
        if not self.ready.wait(3):
            self.error = "network observer initialization timed out"
            self.close()

    def run(self):
        refs = []
        callbacks = []  # Keep ctypes callback objects alive until unscheduled.
        scheduled = []
        try:
            sc = ctypes.CDLL("/System/Library/Frameworks/SystemConfiguration.framework/SystemConfiguration")
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.cf = cf
            callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)
            sc.SCNetworkReachabilityCreateWithAddress.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            sc.SCNetworkReachabilityCreateWithAddress.restype = ctypes.c_void_p
            sc.SCNetworkReachabilityGetFlags.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            sc.SCNetworkReachabilityGetFlags.restype = ctypes.c_bool
            sc.SCNetworkReachabilitySetCallback.argtypes = [ctypes.c_void_p, callback_type, ctypes.c_void_p]
            sc.SCNetworkReachabilitySetCallback.restype = ctypes.c_bool
            for name in ("SCNetworkReachabilityScheduleWithRunLoop", "SCNetworkReachabilityUnscheduleFromRunLoop"):
                fn = getattr(sc, name)
                fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
                fn.restype = ctypes.c_bool
            cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
            cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
            cf.CFRunLoopRunInMode.restype = ctypes.c_int
            cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode").value
            with self.lock:
                self.run_loop = cf.CFRunLoopGetCurrent()
            values = [None, None]

            def update(index, flags):
                values[index] = usable(flags)
                available = True if True in values else False if all(v is False for v in values) else None
                self.gate.update(available)

            for index, (family, size) in enumerate(((socket.AF_INET, 16), (socket.AF_INET6, 28))):
                # Darwin sockaddr starts with its byte length and address family.
                address = ctypes.create_string_buffer(bytes((size, family)) + bytes(size - 2), size)
                ref = sc.SCNetworkReachabilityCreateWithAddress(None, address)
                if not ref:
                    raise OSError("cannot create default-route observer")
                refs.append(ref)
                callback = callback_type(lambda target, flags, context, i=index: update(i, flags))
                callbacks.append(callback)
                if (not sc.SCNetworkReachabilitySetCallback(ref, callback, None) or
                        not sc.SCNetworkReachabilityScheduleWithRunLoop(ref, self.run_loop, mode)):
                    raise OSError("cannot schedule default-route observer")
                scheduled.append(ref)
                flags = ctypes.c_uint32()
                if not sc.SCNetworkReachabilityGetFlags(ref, ctypes.byref(flags)):
                    raise OSError("cannot read default-route availability")
                update(index, flags.value)
            self.ready.set()
            while not self.stopping.is_set():
                result = cf.CFRunLoopRunInMode(mode, 3600, False)
                if result == 1 and not self.stopping.is_set():
                    raise OSError("network observer lost its notification sources")
        except (OSError, AttributeError, ValueError) as exc:
            self.error = str(exc)
            self.gate.update(None)
        finally:
            for ref in scheduled:
                sc.SCNetworkReachabilityUnscheduleFromRunLoop(ref, self.run_loop, mode)
            for ref in refs:
                cf.CFRelease(ref)
            with self.lock:
                self.run_loop = None
            self.ready.set()

    def close(self):
        self.stopping.set()
        with self.lock:
            loop = self.run_loop
            if loop is not None:
                self.cf.CFRunLoopStop(loop)
        self.thread.join(timeout=4)
        if self.thread.is_alive():
            raise OSError("network observer did not stop")


_gate = Gate()


def get():
    return _gate


def require(host):
    get().require(host)


@contextmanager
def observe():
    # Programmatic callers can supply Gate events themselves. Only real CLI
    # entrypoints start native observers, keeping library/unit use deterministic.
    observer = DarwinMonitor(get()) if sys.platform == "darwin" else None
    if observer is not None:
        observer.start()
        if observer.error:
            print("kzw: network observer unavailable; automatic offline pause is disabled: " + observer.error,
                  file=sys.stderr, flush=True)
    try:
        yield
    finally:
        if observer is not None:
            observer.close()
