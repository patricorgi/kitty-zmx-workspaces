import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kzw import remote_client
from kzw.remote import Conflict, Store
from test_layout_prune import A, B, C, snapshot

R = 'kzw-' + 'd' * 32
TOKEN = 'f' * 32


def portable_snapshot():
    text = snapshot()
    for pane in (A, B, C):
        text = text.replace('KZW_PANE_EXECUTABLE --pane ' + pane,
                            'KZW_PANE_EXECUTABLE --pane ' + pane + ' --host KZW_ORIGIN --existing')
    return text


class DetachedReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.directory = root / 'Test'
        self.directory.mkdir()
        self.store = Store(root / 'remote.sqlite')
        self.addCleanup(self.store.close)
        self.store.db.execute('INSERT INTO workspaces VALUES (?,?,?,?)', ('Test', 1, portable_snapshot(), json.dumps([A, B, C])))
        self.store.db.execute('INSERT INTO leases VALUES (?,?,?)', ('Test', TOKEN, time.time() + 100))
        self.store.db.execute('INSERT INTO reservations VALUES (?,?)', (R, 'Test'))
        remote_client.save_config(self.directory, {'workspace': 'Test', 'host': 'origin', 'state_home': '', 'revision': 1, 'panes': [A, B, C]})
        (self.directory / 'lease.json').write_text(json.dumps({'token': TOKEN}))
        pending = self.directory / 'native-pending'
        pending.mkdir(mode=0o700)
        (pending / (R + '.json')).write_text(json.dumps({'pane': R, 'host': 'origin', 'workspace': 'Test'}) + '\n')
        self.rpc = patch('kzw.remote_client.call', side_effect=lambda host, req: self.store.dispatch(req))
        self.rpc.start()
        self.addCleanup(self.rpc.stop)
        self.listing = patch('kzw.remote.subprocess.run', return_value=subprocess.CompletedProcess([], 0, A + '\n' + C + '\n', ''))
        self.listing.start()
        self.addCleanup(self.listing.stop)

    def test_reconciles_missing_and_abandoned_reservation(self):
        remote_client.pull(self.directory, 'Test', 'origin', '/bin/kzw-pane')
        result = self.store.dispatch({'method': 'workspace.get', 'workspace': 'Test'})
        self.assertEqual(result['panes'], [A, C])
        self.assertEqual(result['reservations'], [])
        self.assertNotIn(B, result['snapshot'])
        self.assertFalse((self.directory / 'native-pending' / (R + '.json')).exists())
        self.assertFalse((self.directory / 'reconcile-pending.json').exists())

    def test_server_rechecks_absence_and_rolls_back(self):
        req = {'method': 'workspace.reconcile', 'workspace': 'Test', 'revision': 1, 'request_id': 'test',
               'snapshot': 'unchanged', 'panes': [A, C], 'remove': [B], 'cancel_reservations': [R], 'lease_token': TOKEN}
        with patch('kzw.remote.subprocess.run', return_value=subprocess.CompletedProcess([], 0, A + '\n' + B + '\n', '')):
            with self.assertRaises(Conflict):
                self.store.dispatch(req)
        self.assertEqual(self.store.dispatch({'method': 'workspace.get', 'workspace': 'Test'})['reservations'], [R])

    def test_failed_listing_does_not_modify_or_create_journal(self):
        with patch('kzw.remote.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['zmx'])):
            with self.assertRaises(subprocess.CalledProcessError):
                remote_client.pull(self.directory, 'Test', 'origin', '/bin/kzw-pane')
        self.assertFalse((self.directory / 'reconcile-pending.json').exists())
        self.assertEqual(self.store.dispatch({'method': 'workspace.get', 'workspace': 'Test'})['panes'], [A, B, C])

    def test_live_reservation_cannot_be_cancelled(self):
        with patch('kzw.remote.subprocess.run', return_value=subprocess.CompletedProcess([], 0, A + '\n' + C + '\n' + R + '\n', '')):
            with self.assertRaises(Conflict):
                remote_client.pull(self.directory, 'Test', 'origin', '/bin/kzw-pane')
        self.assertEqual(self.store.dispatch({'method': 'workspace.get', 'workspace': 'Test'})['reservations'], [R])
        self.assertTrue((self.directory / 'native-pending' / (R + '.json')).exists())

    def test_staged_layout_blocks_reconciliation(self):
        self.store.db.execute('INSERT INTO staged_layouts VALUES (?,?,?,?)', ('Test', 1, 'staged', json.dumps([A, B, C, R])))
        req = {'method': 'workspace.reconcile', 'workspace': 'Test', 'revision': 1, 'request_id': 'test',
               'snapshot': 'unchanged', 'panes': [A, C], 'remove': [B], 'cancel_reservations': [R], 'lease_token': TOKEN}
        with self.assertRaises(Conflict):
            self.store.dispatch(req)
        self.assertEqual(self.store.dispatch({'method': 'workspace.get', 'workspace': 'Test'})['reservations'], [R])

    def test_lost_reply_replays_same_request(self):
        dropped = [False]
        def call(host, request):
            result = self.store.dispatch(request)
            if request['method'] == 'workspace.reconcile' and not dropped[0]:
                dropped[0] = True
                raise OSError('lost reply')
            return result
        with patch('kzw.remote_client.call', side_effect=call):
            with self.assertRaises(OSError):
                remote_client.pull(self.directory, 'Test', 'origin', '/bin/kzw-pane')
            self.assertTrue((self.directory / 'reconcile-pending.json').exists())
            remote_client.pull(self.directory, 'Test', 'origin', '/bin/kzw-pane')
        self.assertEqual(self.store.dispatch({'method': 'workspace.get', 'workspace': 'Test'})['revision'], 2)
