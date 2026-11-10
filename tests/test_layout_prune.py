import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

from kzw.layout_prune import prune_snapshot

A, B, C = ("kzw-" + ch * 32 for ch in "abc")


def snapshot():
    state = {"class": "Splits", "opts": {}, "pairs": {"one": 1, "two": {"horizontal": False, "one": 2, "two": 3}},
             "all_windows": {"active_group_idx": 1, "active_group_history": [1, 2, 3],
                             "window_groups": [{"id": i, "window_ids": [i]} for i in (1, 2, 3)]}}
    lines = ['# kzw-focus ' + json.dumps({"pane": B}), 'new_tab Test', 'layout splits',
             'set_layout_state ' + json.dumps(state)]
    for i, pane in enumerate((A, B, C), 1):
        lines.append(shlex.join(['launch', 'kitty-unserialize-data=' + json.dumps({'id': i}),
                                 '--var=kzw_pane=' + pane, 'KZW_PANE_EXECUTABLE', '--pane', pane]))
    return '\n'.join(lines) + '\n'


class LayoutPruneTests(unittest.TestCase):
    def test_collapse_missing_branch_preserving_survivors(self):
        output = prune_snapshot(snapshot(), {B})
        state = json.loads(next(line[len('set_layout_state '):] for line in output.splitlines()
                                if line.startswith('set_layout_state ')))
        self.assertEqual(state['pairs'], {'one': 1, 'two': 3})
        self.assertEqual([g['id'] for g in state['all_windows']['window_groups']], [1, 3])
        self.assertEqual(state['all_windows']['active_group_idx'], 1)
        self.assertTrue(output.startswith('# kzw-focus ' + json.dumps({'pane': C}, separators=(',', ':'))))
        self.assertNotIn(B, output)
        self.assertIn(A, output)
        self.assertIn(C, output)

    def test_empty_and_invalid_pruning(self):
        self.assertEqual(prune_snapshot(snapshot(), {A, B, C}), '# kzw-empty-workspace\n')
        for name, data, missing in (
            ('unknown pane', snapshot(), {'kzw-' + 'd' * 32}),
            ('unsupported layout', snapshot().replace('layout splits', 'layout tall'), {B}),
        ):
            with self.subTest(case=name), self.assertRaises(ValueError):
                prune_snapshot(data, missing)

    @unittest.skipUnless(os.environ.get('KZW_RUN_INTEGRATION') == '1', 'real integration is opt-in')
    def test_pruned_tree_restores_in_hidden_kitty(self):
        if not shutil.which('kitty'):
            self.skipTest('kitty is required')
        with tempfile.TemporaryDirectory(prefix='kp-', dir=Path.home() / '.cache') as tmp:
            session = Path(tmp) / 'Test.session'
            lines = []
            for line in prune_snapshot(snapshot(), {B}).splitlines():
                if line.startswith('launch '):
                    args = shlex.split(line)
                    line = shlex.join(args[:3] + ['/bin/sh'])
                lines.append(line)
            session.write_text('\n'.join(lines) + '\n')
            sock = Path(tmp) / 'kitty.sock'
            proc = subprocess.Popen(['kitty', '--start-as=hidden', '--listen-on', 'unix:' + str(sock),
                                     '-c', 'NONE', '-o', 'allow_remote_control=socket-only', '--session', str(session)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def command(*args):
                return subprocess.run(['kitty', '@', '--to', 'unix:' + str(sock), *args], capture_output=True,
                                      text=True, check=True, timeout=10).stdout
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        tree = json.loads(command('ls'))
                        windows = [w for osw in tree for tab in osw['tabs'] for w in tab['windows']]
                        if len(windows) == 2:
                            break
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    time.sleep(.1)
                else:
                    self.fail('hidden Kitty did not restore the pruned session')
                self.assertEqual({w['user_vars']['kzw_pane'] for w in windows}, {A, C})
                saved = command('ls', '--output-format=session')
                self.assertIn('layout splits', saved)
                self.assertEqual(sum(line.startswith('launch ') for line in saved.splitlines()), 2)
                state = json.loads(next(line[len('set_layout_state '):] for line in saved.splitlines()
                                        if line.startswith('set_layout_state ')))
                ids = {}
                for line in saved.splitlines():
                    if line.startswith('launch '):
                        args = shlex.split(line)
                        wid = json.loads(args[1][len('kitty-unserialize-data='):])['id']
                        ids[wid] = next(x[len('--var=kzw_pane='):] for x in args if x.startswith('--var=kzw_pane='))
                group_panes = {g['id']: ids[g['window_ids'][0]] for g in state['all_windows']['window_groups']}
                self.assertEqual(group_panes[state['pairs']['one']], A)
                self.assertEqual(group_panes[state['pairs']['two']], C)
            finally:
                if proc.poll() is None:
                    try:
                        command('close-window', '--match', 'all')
                        proc.wait(timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        proc.terminate()
                        proc.wait(timeout=10)
