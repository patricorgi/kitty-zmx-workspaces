import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kzw import remote, remote_client


class HostMetricsTests(unittest.TestCase):
    def test_shared_sampling_cpu_delta_and_no_guest_double_count(self):
        meter = remote.HostMetrics()
        with patch.object(meter, "counters", side_effect=[(100, 80, 1000, 400), (200, 130, 1000, 250)]) as read, \
             patch("kzw.remote.time.monotonic", return_value=0) as clock:
            first = meter.get()
            self.assertIsNone(first["cpu_percent"])
            self.assertEqual(first["ram_percent"], 60)
            clock.return_value = 4
            with ThreadPoolExecutor(max_workers=8) as pool:
                samples = list(pool.map(lambda _: meter.get(), range(20)))
            self.assertTrue(all(s["age_seconds"] == 4 for s in samples))
            self.assertEqual(read.call_count, 1)
            clock.return_value = 5
            second = meter.get()
            self.assertEqual((second["cpu_percent"], second["ram_percent"], second["age_seconds"]), (50, 75, 0))
            self.assertEqual(read.call_count, 2)

    def test_failure_counter_reset_and_recovery_never_report_stale_cpu(self):
        meter = remote.HostMetrics()
        readings = [(100, 80, 1000, 400), OSError("offline"), (300, 230, 1000, 200),
                    (10, 5, 1000, 200), (110, 65, 1000, 200)]
        with patch.object(meter, "counters", side_effect=readings), patch("kzw.remote.time.monotonic") as clock:
            values = []
            for tick in range(5):
                clock.return_value = tick * 5
                values.append(meter.get())
        self.assertEqual([s["cpu_percent"] for s in values], [None, None, None, None, 40])
        self.assertIsNone(values[1]["ram_percent"])

    def test_linux_counters_use_available_memory_and_ignore_guest_fields(self):
        files = {"/proc/stat": "cpu 100 20 30 400 10 4 6 2 80 10\ncpu0 1 2 3 4\n",
                 "/proc/meminfo": "MemTotal: 1000 kB\nMemFree: 50 kB\nMemAvailable: 350 kB\n"}
        with patch("kzw.remote.sys.platform", "linux"), patch("builtins.open", side_effect=lambda path: io.StringIO(files[path])):
            self.assertEqual(remote.HostMetrics().counters(), (572, 410, 1000, 350))

    def test_metrics_do_not_write_database_or_query_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = [remote.Store(Path(tmp) / "coordinator.sqlite") for _ in range(2)]
            try:
                meter = remote.HostMetrics()
                with patch("kzw.remote.HOST_METRICS", meter), patch.object(meter, "counters", return_value=(10, 2, 100, 40)) as read, \
                     patch("kzw.remote.subprocess.run", side_effect=AssertionError("unexpected process")):
                    before = list(stores[0].db.iterdump())
                    for store in stores:
                        self.assertEqual(store.dispatch({"method": "host.metrics"})["ram_percent"], 60)
                    self.assertEqual(list(stores[0].db.iterdump()), before)
                    read.assert_called_once_with()
            finally:
                for store in stores:
                    store.close()

    def test_watch_metrics_are_opt_in_and_do_not_change_status_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = remote.Store(Path(tmp) / "coordinator.sqlite")
            self.addCleanup(store.close)
            status = {"revision": 1, "live": [], "missing": [], "confirmed_missing": []}
            dispatch = store.dispatch
            def request(req):
                if req["method"] == "workspace.status":
                    return dict(status)
                return dispatch(req)
            with patch.object(store, "dispatch", side_effect=request), patch.object(remote.HOST_METRICS, "get", return_value={"cpu_percent": 25}) as metrics:
                ordinary = request({"method": "workspace.watch", "workspace": "Test", "timeout": 0})
                self.assertEqual(ordinary, status)
                metrics.assert_not_called()
                sampled = request({"method": "workspace.watch", "workspace": "Test", "known": status, "timeout": 0, "host_metrics": True})
                self.assertEqual(sampled.pop("host_metrics"), {"cpu_percent": 25})
                self.assertEqual(sampled, status)


class MetricsMonitorTests(unittest.TestCase):
    def test_metrics_use_existing_watch_stream_without_entering_known_status_or_journal(self):
        for has_metrics, callback_fails in ((True, False), (False, False), (True, True)):
            with self.subTest(metrics=has_metrics, callback_fails=callback_fails), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                (directory / "remote.json").write_text(json.dumps({"host": "origin", "workspace": "Test", "state_home": ""}))
                requests, received = [], []
                done, release = threading.Event(), threading.Event()
                sample = {"host": "remote", "cpu_percent": 50, "ram_percent": 60, "age_seconds": 0}
                status = {"revision": 1, "live": [], "missing": [], "confirmed_missing": []}

                class Channel:
                    process = Mock(poll=lambda: None, terminate=release.set)

                    def __init__(self, command):
                        pass

                    def request(self, request, timeout):
                        requests.append(request)
                        if request["method"] == "workspace.events":
                            return {"events": []}
                        if request["known"] is not None:
                            done.set()
                            release.wait(3)
                            raise OSError("closed")
                        return {**status, **({"host_metrics": sample} if has_metrics else {})}

                    def close(self):
                        pass

                def receive(value):
                    received.append(value)
                    if callback_fails:
                        raise OSError("Kitty unavailable")

                with patch("kzw.remote_client.Channel", Channel), patch.dict("os.environ", {"KZW_REMOTE_STATE_HOME": ""}):
                    close = remote_client.start_status_monitor(directory, on_metrics=receive)
                    try:
                        self.assertTrue(done.wait(3))
                        self.assertEqual(received, [sample if has_metrics else None])
                        watches = [r for r in requests if r["method"] == "workspace.watch"]
                        self.assertTrue(all(r["host_metrics"] for r in watches))
                        self.assertEqual(watches[-1]["known"], status)
                        self.assertNotIn("host_metrics", json.loads((directory / "remote-status.changed").read_text()))
                    finally:
                        close()
