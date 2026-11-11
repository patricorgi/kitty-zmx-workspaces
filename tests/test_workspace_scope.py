import json
import itertools
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kzw import cli

PANE = 'kzw-' + 'a' * 32


def os_window(os_id, pane=None):
    vars = {'kzw_pane': pane, 'kzw_host': ''} if pane else {}
    return {'id': os_id, 'last_focused': False, 'tabs': [{'id': os_id, 'is_active': True,
            'windows': [{'id': os_id, 'is_active': True, 'user_vars': vars}]}]}


class WorkspaceScopeTests(unittest.TestCase):
    def test_unmanaged_os_window_is_excluded(self):
        result = subprocess.CompletedProcess([], 0, json.dumps([os_window(1, PANE), os_window(2)]), '')
        with patch('kzw.cli.subprocess.run', return_value=result):
            scoped = json.loads(cli.query('/socket', 'ls'))
        self.assertEqual([item['id'] for item in scoped], [1])
        self.assertTrue(scoped[0]['last_focused'])

    def test_multiple_managed_os_windows_fail_closed(self):
        result = subprocess.CompletedProcess([], 0, json.dumps([os_window(1, PANE), os_window(2, PANE)]), '')
        with patch('kzw.cli.subprocess.run', return_value=result), self.assertRaises(ValueError):
            cli.query('/socket', 'ls')

    def test_explicit_os_window_scope_ignores_other_workspaces(self):
        other = 'kzw-' + 'b' * 32
        tree = [os_window(1, PANE), os_window(2, other), os_window(3)]
        result = subprocess.CompletedProcess([], 0, json.dumps(tree), '')
        with patch('kzw.cli.subprocess.run', return_value=result):
            self.assertEqual([w['id'] for w in json.loads(cli.query('/socket', 'ls', workspace_os_id=1))], [1])
            self.assertEqual([w['id'] for w in json.loads(cli.query('/socket', 'ls', workspace_os_id=2))], [2])
            self.assertEqual(json.loads(cli.query('/socket', 'ls', workspace_os_id=4)), [])
            with self.assertRaisesRegex(ValueError, 'invalid workspace'):
                cli.query('/socket', 'ls', workspace_os_id=True)

    def test_scoped_export_rejects_unmanaged_content(self):
        target = os_window(1, PANE)
        target['tabs'][0]['windows'].append({'id': 9, 'user_vars': {}})
        result = subprocess.CompletedProcess([], 0, json.dumps([target, os_window(2, PANE)]), '')
        with patch('kzw.cli.subprocess.run', return_value=result), self.assertRaisesRegex(ValueError, 'not managed'):
            cli.query('/socket', 'ls', '--output-format=session', workspace_os_id=1)

    def test_unmanaged_pane_inside_workspace_prevents_export(self):
        workspace = os_window(1, PANE)
        workspace['tabs'][0]['windows'].append({'id': 9, 'user_vars': {}})
        result = subprocess.CompletedProcess([], 0, json.dumps([workspace, os_window(2)]), '')
        with patch('kzw.cli.subprocess.run', return_value=result), self.assertRaisesRegex(ValueError, 'not managed'):
            cli.query('/socket', 'ls', '--output-format=session')

    def test_scoped_export_targets_anchor_and_checks_identity(self):
        tree = subprocess.CompletedProcess([], 0, json.dumps([os_window(1, PANE), os_window(2)]), '')
        export = subprocess.CompletedProcess([], 0, json.dumps({'os_window_id': 1, 'session': 'new_tab Test\n'}), '')
        with patch('kzw.cli.subprocess.run', side_effect=[tree, export]) as run:
            self.assertEqual(cli.query('/socket', 'ls', '--output-format=session'), 'new_tab Test\n')
        self.assertIn('id:1', run.call_args_list[1].args[0])

    def test_supervisor_releases_without_closing_utility_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'remote.json').write_text('{}')
            child = Mock(returncode=None)
            child.poll.return_value = None
            ticks = itertools.count()
            with patch('kzw.cli.state_dir', return_value=root), \
                 patch('kzw.cli.shutil.which', return_value='/bin/kzw-pane'), \
                 patch('kzw.cli.prepare_bootstrap', return_value=root / 'bootstrap.kitty-session'), \
                 patch('kzw.cli.mark_bootstrap_launching'), patch('kzw.cli.finish_bootstrap'), \
                 patch('kzw.cli.start_title_monitor', return_value=Mock()), \
                 patch('kzw.cli.remote_client.start_status_monitor', return_value=lambda: None), \
                 patch('kzw.cli.remote_client.ConnectionBroker') as broker, \
                 patch('kzw.cli.subprocess.Popen', return_value=child), \
                 patch('kzw.cli.supervisor_window.source_window', return_value=None), \
                 patch('kzw.cli.synchronize_exits'), patch('kzw.cli.capture'), \
                 patch('kzw.cli.query', return_value='[]'), patch('kzw.cli.time.sleep'), \
                 patch('kzw.cli.time.monotonic', side_effect=lambda: next(ticks)):
                broker.return_value.path = root / 'rpc.sock'
                self.assertEqual(cli.run('Test', 'origin', remote_attached=True, new_instance=True), 0)
            child.terminate.assert_not_called()
            child.kill.assert_not_called()
            child.wait.assert_not_called()

    @unittest.skipUnless(os.environ.get('KZW_RUN_INTEGRATION') == '1', 'real integration is opt-in')
    def test_hidden_kitty_utility_window_is_not_exported_or_closed(self):
        if not shutil.which('kitty'):
            self.skipTest('kitty is required')
        with tempfile.TemporaryDirectory(prefix='ks-', dir=Path.home() / '.cache') as tmp:
            root = Path(tmp)
            session = root / 'Test.session'
            session.write_text('new_tab Test\nlaunch --var=kzw_pane=' + PANE + ' --var=kzw_host= /bin/sh\n')
            sock = root / 'kitty.sock'
            proc = subprocess.Popen(['kitty', '--start-as=hidden', '--listen-on', 'unix:' + str(sock),
                                     '-c', 'NONE', '-o', 'allow_remote_control=socket-only', '--session', str(session)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def raw(*args):
                return subprocess.run(['kitty', '@', '--to', 'unix:' + str(sock), *args], text=True,
                                      capture_output=True, check=True, timeout=10).stdout
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        scoped = json.loads(cli.query(sock, 'ls'))
                        if scoped:
                            break
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    time.sleep(.1)
                else:
                    self.fail('hidden Kitty workspace did not start')
                utility = int(raw('launch', '--type=os-window', '--os-window-state=minimized', '/bin/sh').strip())
                scoped = json.loads(cli.query(sock, 'ls'))
                self.assertEqual(len(scoped), 1)
                self.assertNotIn(utility, cli.pane_map(scoped))
                self.assertEqual(cli.pane_map(scoped), {1: (PANE, '')})
                workspace_os_id = scoped[0]['id']
                other_pane = 'kzw-' + 'c' * 32
                other = int(raw('launch', '--type=os-window', '--os-window-state=minimized',
                                '--var=kzw_pane=' + other_pane, '--var=kzw_host=', '/bin/sh').strip())
                with self.assertRaisesRegex(ValueError, 'multiple Kitty OS windows'):
                    cli.query(sock, 'ls')
                scoped = json.loads(cli.query(sock, 'ls', workspace_os_id=workspace_os_id))
                self.assertEqual(cli.pane_map(scoped), {1: (PANE, '')})
                scoped_export = cli.query(sock, 'ls', '--output-format=session', workspace_os_id=workspace_os_id)
                self.assertEqual(sum(line.startswith('launch ') for line in scoped_export.splitlines()), 1)
                self.assertNotIn(other_pane, scoped_export)
                raw('close-window', '--match', f'id:{other}')
                exported = cli.query(sock, 'ls', '--output-format=session')
                self.assertEqual(sum(line.startswith('launch ') for line in exported.splitlines()), 1)
                second_pane = 'kzw-' + 'b' * 32
                second = int(raw('launch', '--match', 'id:1', '--type=tab', '--var=kzw_pane=' + second_pane,
                                 '--var=kzw_host=', '/bin/sh').strip())
                self.assertEqual(cli.pane_map(json.loads(cli.query(sock, 'ls'))),
                                 {1: (PANE, ''), second: (second_pane, '')})
                (root / 'panes.json').write_text(json.dumps([PANE]))
                with patch('kzw.cli.zmx_command', return_value=PANE + '\n' + second_pane + '\n'), \
                     patch('kzw.cli.state_dir', return_value=root), \
                     patch('kzw.cli.shutil.which', return_value='/bin/kzw-pane'), \
                     patch('kzw.cli.remote_client.publish'):
                    cli.capture_locked(root, '/bin/kzw-pane')
                    self.assertEqual(set(json.loads((root / 'panes.json').read_text())), {PANE, second_pane})
                    cli.detach('Test')
                self.assertIsNone(proc.poll())
                tree = json.loads(raw('ls'))
                remaining = [w['id'] for osw in tree for tab in osw['tabs'] for w in tab['windows']]
                self.assertEqual(remaining, [utility])
            finally:
                if proc.poll() is None:
                    try:
                        raw('close-window', '--match', 'all')
                        proc.wait(timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        proc.terminate()
                        proc.wait(timeout=10)
