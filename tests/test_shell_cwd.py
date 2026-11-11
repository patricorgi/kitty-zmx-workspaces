import io
import importlib
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from kzw import pane, shell_cwd

PANE = 'kzw-' + 'a' * 32


class ShellCwdTests(unittest.TestCase):
    def test_create_kitten_decodes_kitty_bytes_cwd(self):
        handler = types.ModuleType('kittens.tui.handler')
        handler.result_handler = lambda **kwargs: lambda func: func
        modules = {'kittens': types.ModuleType('kittens'),
                   'kittens.tui': types.ModuleType('kittens.tui'),
                   'kittens.tui.handler': handler}
        with patch.dict(sys.modules, modules), patch.dict('os.environ', {'KZW_WORKSPACE': 'Test'}, clear=True):
            sys.modules.pop('kzw.create_kitten', None)
            kitten = importlib.import_module('kzw.create_kitten')
            window = types.SimpleNamespace(user_vars={'kzw_pane': PANE},
                                           screen=types.SimpleNamespace(last_reported_cwd=b'file://host/tmp'))
            boss = types.SimpleNamespace(window_id_map={1: window})
            with patch.object(kitten.subprocess, 'Popen') as popen:
                kitten.handle_result(['create', 'tab'], None, 1, boss)
            self.assertIn('file://host/tmp', popen.call_args.args[0])
            sys.modules.pop('kzw.create_kitten', None)

    def test_local_cwd_must_match_local_host(self):
        with patch('kzw.shell_cwd.socket.gethostname', return_value='host'), \
             patch('kzw.shell_cwd.socket.getfqdn', return_value='host.example'):
            self.assertEqual(shell_cwd.resolve('file://host/tmp/a%20b', ''), '/tmp/a b')
            self.assertEqual(shell_cwd.resolve('kitty-shell-cwd://host/tmp/a%20b', ''), '/tmp/a%20b')
            with self.assertRaises(ValueError):
                shell_cwd.resolve('file://other/tmp/a', '')
            with self.assertRaises(ValueError):
                shell_cwd.resolve('file://host/tmp/a%0Ab', '')

    def test_remote_cwd_requires_verified_origin_hostname(self):
        result = subprocess.CompletedProcess([], 0, 'remote\nremote.example\n', '')
        with patch('kzw.shell_cwd.subprocess.run', return_value=result) as run:
            self.assertEqual(shell_cwd.resolve('file://remote.example/home/user/work', 'origin'), '/home/user/work')
            with self.assertRaises(ValueError):
                shell_cwd.resolve('file://other/home/user/work', 'origin')
        self.assertEqual(run.call_args.args[0][:5], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5'])

    def test_cwd_validation_rejects_malformed_reports_before_querying_origin(self):
        cases = (None, b'file://host/tmp', 'x' * 8193, '/tmp', 'https://host/tmp',
                 'file:///tmp', 'file://user@host/tmp', 'file://user:pass@host/tmp',
                 'file://host:22/tmp', 'file://host/tmp?q=1', 'file://host/tmp#part',
                 'file://host/tmp/%00', 'file://host/tmp/%7f', 'file://host/tmp/%ff',
                 'kitty-shell-cwd://host', 'kitty-shell-cwd://bad@host/tmp',
                 'kitty-shell-cwd://host/tmp/\x00')
        with patch('kzw.shell_cwd.subprocess.run') as run:
            for uri in cases:
                with self.subTest(uri=repr(uri)[:100]), self.assertRaises(ValueError):
                    shell_cwd.resolve(uri, 'origin')
            with self.assertRaises(ValueError):
                shell_cwd.resolve('file://host/tmp', '-origin')
            run.assert_not_called()

    def test_cwd_host_normalization_and_failed_verification(self):
        with patch('kzw.shell_cwd.socket.gethostname', return_value='Host.'), \
             patch('kzw.shell_cwd.socket.getfqdn', return_value='Host.Example.'):
            for name in ('HOST.', 'host.example', 'localhost'):
                with self.subTest(name=name):
                    self.assertEqual(shell_cwd.resolve('file://' + name + '/tmp/a%20b', ''), '/tmp/a b')
        for failure in (OSError('offline'), subprocess.TimeoutExpired('ssh', 10),
                        subprocess.CalledProcessError(255, 'ssh')):
            with self.subTest(failure=type(failure).__name__), \
                 patch('kzw.shell_cwd.subprocess.run', side_effect=failure), self.assertRaises(type(failure)):
                shell_cwd.resolve('file://origin/tmp', 'origin')

    def test_new_remote_session_quotes_cwd_without_injecting_commands(self):
        cwd = "/tmp/a b';touch /tmp/not-run"
        with patch.dict('os.environ', {}, clear=True), \
             patch('sys.argv', ['kzw-pane', '--pane', PANE, '--host', 'origin', '--stage-new', '--initial-cwd', cwd]), \
             patch('sys.stdout', new_callable=io.StringIO), patch('kzw.pane.report_attachment_ended'), \
             patch('kzw.pane.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            pane.main()
        import shlex
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ['ssh', '-t', 'origin'])
        self.assertEqual(shlex.split(cmd[3])[-1], cwd)

    def test_existing_session_rejects_initial_cwd(self):
        with patch('sys.argv', ['kzw-pane', '--pane', PANE, '--existing', '--stage-new', '--initial-cwd', '/tmp']), \
             patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            pane.main()

    def test_ssh_mux_noise_is_filtered_but_failures_remain_visible(self):
        noisy = ('mux_client_request_session: session request failed: Session open refused by peer\n'
                 'ControlSocket /tmp/example already exists, disabling multiplexing\n'
                 'Permission denied (publickey).\n')
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            result = pane.run_attachment(['/bin/sh', '-c', 'printf %s "$1" >&2', 'sh', noisy], {}, True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(stderr.getvalue(), 'Permission denied (publickey).\n')
