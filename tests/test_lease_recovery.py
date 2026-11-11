import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from kzw import remote_client
from kzw.remote import Store, response
from workspace_fixtures import layout


PANE = "kzw-" + "a" * 32


class LeaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.root / "server" / "state.sqlite"
        self.store = Store(self.database)
        self.addCleanup(self.store.close)
        self.directory = self.root / "client"
        self.directory.mkdir()
        listing = patch("kzw.remote.subprocess.run", return_value=subprocess.CompletedProcess([], 0, PANE + "\n", ""))
        listing.start()
        self.addCleanup(listing.stop)
        calls = patch("kzw.remote_client.call", side_effect=lambda host, req: self.request(req))
        calls.start()
        self.addCleanup(calls.stop)
        env = patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": ""})
        env.start()
        self.addCleanup(env.stop)
        remote_client.create(self.directory, "Test", "origin")
        self.token = remote_client.acquire_lease(self.directory, "Test", "origin")
        self.identity = ("origin", "Test", "")

    def request(self, request):
        return remote_client.decode_response(json.dumps(response(self.store, json.dumps(request))))

    def expire(self):
        self.store.db.execute("UPDATE leases SET expires=1 WHERE workspace='Test'")

    def maintain(self, request=None):
        return remote_client.maintain_lease(self.directory, request or self.request, self.identity)

    def test_rotation_preserves_layout_and_fences_old_token(self):
        before = self.request({"method": "workspace.sync", "workspace": "Test"})
        config = (self.directory / "remote.json").read_bytes()
        self.expire()
        self.assertTrue(self.maintain())
        current = remote_client.lease_token(self.directory)
        self.assertNotEqual(current, self.token)
        with self.assertRaises(remote_client.RemoteRejected):
            self.request({"method": "workspace.lease.renew", "workspace": "Test", "token": self.token})
        self.assertFalse(self.maintain())
        self.assertEqual(self.request({"method": "workspace.sync", "workspace": "Test"}), before)
        self.assertEqual((self.directory / "remote.json").read_bytes(), config)

    def test_recovery_cannot_take_an_intervening_owner_or_revive_release(self):
        recovery = {"method": "workspace.lease.recover", "workspace": "Test",
                    "previous_token": self.token, "token": "b" * 32}
        for owner, expires in ((self.token, 9999999999), (self.token, 0), ("c" * 32, 9999999999),
                               ("c" * 32, 1), (None, None)):
            with self.subTest(owner=owner, expires=expires):
                self.store.db.execute("DELETE FROM leases")
                if owner is not None:
                    self.store.db.execute("INSERT INTO leases VALUES (?,?,?)", ("Test", owner, expires))
                with self.assertRaises(remote_client.RemoteRejected):
                    self.request(recovery)
                self.assertEqual(self.store.db.execute("SELECT token,expires FROM leases").fetchone(),
                                 (owner, expires) if owner else None)

    def test_competing_acquire_and_recovery_are_atomic(self):
        self.expire()
        barrier = threading.Barrier(2)
        requests = [{"method": "workspace.lease.recover", "workspace": "Test",
                     "previous_token": self.token, "token": "b" * 32},
                    {"method": "workspace.lease.acquire", "workspace": "Test", "token": "c" * 32}]

        def send(request):
            store = Store(self.database)
            try:
                barrier.wait(timeout=3)
                return response(store, json.dumps(request))
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, requests))
        self.assertEqual(sum(result["ok"] for result in results), 1)
        winner = next(i for i, result in enumerate(results) if result["ok"])
        self.assertEqual(self.store.db.execute("SELECT token FROM leases").fetchone()[0], requests[winner]["token"])

    def test_uncertain_rotation_replays_journal_after_transport_or_disk_failure(self):
        for failure in ("before-commit", "after-commit", "local-write"):
            with self.subTest(failure=failure):
                self.expire()
                old = remote_client.lease_token(self.directory)
                original_write = remote_client.atomic_write

                def request(req):
                    if req["method"] == "workspace.lease.recover":
                        if failure == "before-commit":
                            raise OSError("offline")
                        result = self.request(req)
                        if failure == "after-commit":
                            raise OSError("reply lost")
                        return result
                    return self.request(req)

                def write(path, text):
                    if failure == "local-write" and path.name == "lease.json" and "recovery_token" not in json.loads(text):
                        raise OSError("disk unavailable")
                    original_write(path, text)

                with patch("kzw.remote_client.atomic_write", side_effect=write), self.assertRaises(OSError):
                    self.maintain(request)
                pending = json.loads((self.directory / "lease.json").read_text())
                self.assertEqual(pending["token"], old)
                self.assertNotEqual(pending["recovery_token"], old)
                self.assertTrue(self.maintain())
                self.assertEqual(remote_client.lease_token(self.directory), pending["recovery_token"])
                self.assertNotIn("recovery_token", json.loads((self.directory / "lease.json").read_text()))

    def test_lost_recovery_reply_can_expire_without_reviving_stale_successor(self):
        self.expire()

        def lost(req):
            result = self.request(req)
            if req["method"] == "workspace.lease.recover":
                raise OSError("reply lost")
            return result

        with self.assertRaises(OSError):
            self.maintain(lost)
        successor = json.loads((self.directory / "lease.json").read_text())["recovery_token"]
        self.expire()
        self.assertTrue(self.maintain())
        self.assertEqual(remote_client.lease_token(self.directory), successor)
        self.assertEqual(self.store.db.execute("SELECT expires FROM leases").fetchone()[0], 1)
        self.assertTrue(self.maintain())
        self.assertNotEqual(remote_client.lease_token(self.directory), successor)
        self.assertFalse(self.maintain())

    def test_pending_layout_replays_once_after_lease_recovery(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                portable = layout(PANE).replace("new_tab Test", "new_tab retry" + str(committed))
                snapshot, panes = remote_client.translate(portable, "/bin/kzw-pane", "origin", False)
                revision = json.loads((self.directory / "remote.json").read_text())["revision"]

                def lost(host, req):
                    if committed:
                        self.request(req)
                    raise OSError("connection lost")

                with patch("kzw.remote_client.call", side_effect=lost), self.assertRaises(OSError):
                    remote_client.publish(self.directory, snapshot, panes)
                pending = json.loads((self.directory / "remote.json").read_text())["pending"]
                self.expire()
                self.maintain()
                self.assertEqual(json.loads((self.directory / "remote.json").read_text())["pending"], pending)
                remote_client.publish(self.directory, snapshot, panes)
                final = self.request({"method": "workspace.get", "workspace": "Test"})
                self.assertEqual((final["revision"], final["panes"]), (revision + 1, [PANE]))
                self.assertNotIn("pending", json.loads((self.directory / "remote.json").read_text()))

    def test_release_resolves_unknown_rotation_without_reacquiring(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                if not (self.directory / "lease.json").exists():
                    remote_client.acquire_lease(self.directory, "Test", "origin")
                self.expire()

                def lost(req):
                    if req["method"] == "workspace.lease.recover":
                        if committed:
                            self.request(req)
                        raise OSError("reply lost")
                    return self.request(req)

                with self.assertRaises(OSError):
                    self.maintain(lost)
                remote_client.release_lease(self.directory)
                self.assertFalse((self.directory / "lease.json").exists())
                self.assertEqual(self.store.db.execute("SELECT expires FROM leases").fetchone()[0], 0)

    def test_external_rotation_and_origin_validation(self):
        remote_client.release_lease(self.directory)
        current = remote_client.acquire_lease(self.directory, "Test", "origin")
        self.assertNotEqual(current, self.token)
        self.assertFalse(self.maintain())
        for key, value in (("host", "different"), ("workspace", "Other"), ("state_home", "/other")):
            with self.subTest(key=key):
                record = json.loads((self.directory / "lease.json").read_text())
                (self.directory / "lease.json").write_text(json.dumps({**record, key: value}))
                request = Mock()
                with self.assertRaises(ValueError):
                    self.maintain(request)
                request.assert_not_called()
                (self.directory / "lease.json").write_text(json.dumps(record))

    def test_token_lock_keeps_release_and_recovery_serialized(self):
        self.expire()
        with remote_client.lease_lock(self.directory):
            with self.assertRaises(BlockingIOError):
                self.maintain()
            self.assertEqual(remote_client.lease_token(self.directory), self.token)
        self.assertTrue(self.maintain())

    def test_heartbeat_bypasses_blocked_rpc_and_inherited_broker(self):
        renewed = threading.Event()
        channel = Mock()
        channel.process.poll.return_value = None

        def request(req, timeout):
            self.assertEqual(req["method"], "workspace.lease.renew")
            self.assertLess(timeout, 30)
            renewed.set()
            return {"held": True}

        channel.request.side_effect = request
        with remote_client._channel_lock, patch("kzw.remote_client.Channel", return_value=channel), \
             patch("kzw.remote_client.call") as broker, \
             patch.dict(os.environ, {"KZW_RPC_SOCKET": "/unreachable", "KZW_WORKSPACE_ORIGIN": "origin", "KZW_WORKSPACE": "Test"}):
            close = remote_client.start_lease_heartbeat(self.directory)
            try:
                self.assertTrue(renewed.wait(3), "heartbeat blocked behind unrelated layout traffic")
            finally:
                close()
            broker.assert_not_called()
        channel.close.assert_called_once()

    def test_heartbeat_reconnects_and_stops_inflight_request(self):
        for failure in (OSError("SSH interrupted"), BlockingIOError("partial nonblocking write")):
            with self.subTest(failure=type(failure).__name__):
                blocked, cancelled, closed = threading.Event(), threading.Event(), threading.Event()
                first, second = Mock(), Mock()
                first.request.side_effect = failure
                second.process.poll.return_value = None
                second.process.terminate.side_effect = cancelled.set
                second.close.side_effect = closed.set

                def request(req, timeout):
                    blocked.set()
                    if not cancelled.wait(3):
                        raise AssertionError("heartbeat shutdown did not cancel its transport")
                    raise OSError("cancelled")

                second.request.side_effect = request
                with patch("kzw.remote_client.Channel", side_effect=[first, second]), \
                     patch("kzw.remote_client.LEASE_HEARTBEAT_INTERVAL", 0.01), patch("sys.stderr", new_callable=io.StringIO):
                    close = remote_client.start_lease_heartbeat(self.directory)
                    try:
                        self.assertTrue(blocked.wait(3))
                    finally:
                        close()
                self.assertTrue(closed.is_set())
                first.close.assert_called_once()
                self.assertEqual(remote_client.lease_token(self.directory), self.token)
