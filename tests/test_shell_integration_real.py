"""Opt-in isolated Kitty/zmx/Bash shell-integration check."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid


@unittest.skipUnless(os.environ.get('KZW_RUN_INTEGRATION') == '1', 'real integration is opt-in')
class ShellIntegrationReal(unittest.TestCase):
    def test_bash_events_cross_zmx_into_kitty(self):
        if not all(shutil.which(x) for x in ('kitty', 'zmx', 'bash')):
            self.skipTest('kitty, zmx, and bash are required')
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='ksh-', dir=Path.home() / '.cache') as tmp:
            d = Path(tmp)
            sock = d / 'kitty.sock'
            zmx_dir = d / 'zmx'
            zmx_dir.mkdir()
            pane = 'kzw-' + uuid.uuid4().hex
            env = os.environ.copy()
            env.update(HOME=tmp, XDG_STATE_HOME=tmp, ZMX_DIR=str(zmx_dir), PS1='KZW_PROMPT> ',
                       PATH=str(root / 'bin') + os.pathsep + env.get('PATH', ''))
            env.pop('ZMX_SESSION', None)
            env.pop('ZMX_SESSION_PREFIX', None)
            proc = subprocess.Popen(['kitty', '--start-as=hidden', '--listen-on', 'unix:' + str(sock),
                                     '-c', 'NONE', '-o', 'allow_remote_control=socket-only',
                                     '-o', 'watcher=' + str(root / 'kzw/watcher.py'),
                                     'zmx', 'attach', pane, '/bin/bash', '--noprofile', '--norc', '-i'],
                                    env=env, cwd=tmp, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def kitty(*args):
                result = subprocess.run(['kitty', '@', '--to', 'unix:' + str(sock), *args],
                                        capture_output=True, text=True, timeout=10)
                if result.returncode:
                    raise ValueError(result.stderr)
                return result.stdout
            def wait(test, message):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        value = test()
                        if value:
                            return value
                    except (OSError, ValueError, subprocess.SubprocessError, KeyError, IndexError):
                        pass
                    time.sleep(.1)
                self.fail(message)
            try:
                wait(lambda: bool(json.loads(kitty('ls'))), 'disposable Kitty did not start')
                kitty('send-text', "PS1='KZW_PROMPT> '\\r")
                wait(lambda: 'KZW_PROMPT> ' in kitty('get-text'), 'disposable Bash did not start')
                kitty('send-text', 'eval "$(kzw sessions bash-hook)"\r')
                wait(lambda: kitty('get-text').count('KZW_PROMPT> ') >= 2, 'hook did not return to prompt')
                kitty('send-text', 'false\r')
                def command_status():
                    tree = json.loads(kitty('ls'))
                    window = tree[0]['tabs'][0]['windows'][0]
                    return window['user_vars'].get('kzw_command_state') == 'exit:1'
                wait(command_status, 'Kitty did not receive the Bash failure status through zmx')
                probe = d / 'cwd_kitten.py'
                probe.write_text('from kittens.tui.handler import result_handler\n'
                                 'def main(args): pass\n'
                                 '@result_handler(no_ui=True)\n'
                                 'def handle_result(args, answer, target_window_id, boss):\n'
                                 ' w = boss.window_id_map[target_window_id]\n'
                                 f' open({str(d / "cwd-report")!r}, "wb").write(w.screen.last_reported_cwd or b"")\n')
                report = d / 'cwd-report'
                kitty('kitten', str(probe))
                uri = wait(lambda: report.read_text() if report.exists() else '',
                           'Kitty did not receive a working-directory report through zmx')
                from kzw.shell_cwd import resolve
                self.assertTrue(uri, 'Kitty did not receive a working-directory report through zmx')
                self.assertEqual(resolve(uri, ''), tmp)
                kitty('send-text', "printf 'KZW_OUTPUT_TOKEN\\n'\r")
                wait(lambda: 'KZW_OUTPUT_TOKEN' in kitty('get-text', '--extent=last_cmd_output'),
                     'Kitty did not recognize the last command output')
            finally:
                listing = subprocess.run(['zmx', 'list', '--short'], env=env, capture_output=True, text=True, timeout=10)
                if listing.returncode == 0 and pane in listing.stdout.splitlines():
                    subprocess.run(['zmx', 'kill', pane], env=env, capture_output=True, text=True, timeout=10, check=True)
                if proc.poll() is None:
                    try:
                        kitty('close-window', '--match', 'all')
                        proc.wait(timeout=10)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        proc.terminate()
                        proc.wait(timeout=10)
