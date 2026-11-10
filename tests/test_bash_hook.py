import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from kzw import session_meta

ROOT = Path(__file__).resolve().parents[1]
PANE = "kzw-" + "a" * 32
PROMPT = b"KZW_TEST_PROMPT> "


class BashHookTests(unittest.TestCase):
    def shell(self, home):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        env = os.environ.copy()
        env.update(HOME=home, XDG_STATE_HOME=home, ZMX_SESSION=PANE, PS1=PROMPT.decode(),
                   PATH=str(ROOT / "bin") + os.pathsep + str(Path(sys.executable).resolve().parent) + os.pathsep + "/usr/bin:/bin")
        env.pop("PROMPT_COMMAND", None)
        proc = subprocess.Popen([shutil.which("bash") or "bash", "--noprofile", "--norc", "-i"], env=env, cwd=home,
                                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        def stop():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        self.addCleanup(stop)
        return proc, master

    def until(self, master, expected, count=1):
        output = bytearray()
        deadline = time.monotonic() + 10
        while output.count(expected) < count and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
        self.assertGreaterEqual(output.count(expected), count, output.decode(errors="replace"))
        return output

    def test_opt_in_records_last_command_for_the_correct_shell(self):
        with tempfile.TemporaryDirectory() as home:
            proc, master = self.shell(home)
            self.until(master, PROMPT)
            os.write(master, b'eval "$(kzw sessions bash-hook)"\n')
            self.until(master, PROMPT)
            os.write(master, b'printf "HOOK_TEST\\n"\n')
            self.until(master, PROMPT)
            with patch.dict(os.environ, {"XDG_STATE_HOME": home}):
                self.assertEqual(session_meta.read(PANE, proc.pid), 'printf "HOOK_TEST\\n"')

    def test_hook_refuses_to_replace_existing_debug_trap(self):
        with tempfile.TemporaryDirectory() as home:
            proc, master = self.shell(home)
            self.until(master, PROMPT)
            os.write(master, b"trap ':' DEBUG\n")
            self.until(master, PROMPT)
            os.write(master, b'eval "$(kzw sessions bash-hook)"\n')
            output = self.until(master, PROMPT)
            self.assertIn(b"existing DEBUG trap", output)
            with patch.dict(os.environ, {"XDG_STATE_HOME": home}):
                self.assertIsNone(session_meta.read(PANE, proc.pid))

    def test_hook_preserves_command_exit_status(self):
        with tempfile.TemporaryDirectory() as home:
            _, master = self.shell(home)
            self.until(master, PROMPT)
            os.write(master, b'eval "$(kzw sessions bash-hook)"\n')
            self.until(master, PROMPT)
            os.write(master, b"false\n")
            self.until(master, PROMPT)
            os.write(master, b"echo STATUS:$?\n")
            output = self.until(master, PROMPT)
            self.assertIn(b"STATUS:1", output)

    def test_existing_prompt_command_is_preserved_and_not_recorded(self):
        with tempfile.TemporaryDirectory() as home:
            proc, master = self.shell(home)
            self.until(master, PROMPT)
            os.write(master, b"PROMPT_COMMAND='printf OLD_PROMPT'\n")
            self.until(master, PROMPT)
            os.write(master, b'eval "$(kzw sessions bash-hook)"\n')
            output = self.until(master, PROMPT)
            self.assertIn(b"OLD_PROMPT", output)
            os.write(master, b'printf "TRACK_ME\\n"\n')
            output = self.until(master, PROMPT)
            self.assertIn(b"OLD_PROMPT", output)
            with patch.dict(os.environ, {"XDG_STATE_HOME": home}):
                self.assertEqual(session_meta.read(PANE, proc.pid), 'printf "TRACK_ME\\n"')

    def test_hook_refuses_functrace_and_readonly_prompt(self):
        for setup, warning in ((b"set -T\n", b"functrace"),
                               (b"readonly PROMPT_COMMAND=''\n", b"readonly")):
            with self.subTest(setup=setup), tempfile.TemporaryDirectory() as home:
                proc, master = self.shell(home)
                self.until(master, PROMPT)
                os.write(master, setup)
                self.until(master, PROMPT)
                os.write(master, b'eval "$(kzw sessions bash-hook)"\n')
                output = self.until(master, PROMPT)
                self.assertIn(warning, output)
                with patch.dict(os.environ, {"XDG_STATE_HOME": home}):
                    self.assertIsNone(session_meta.read(PANE, proc.pid))

    def test_bash5_prompt_array_is_preserved(self):
        bash = shutil.which("bash")
        if not bash or subprocess.run([bash, "-c", "(( BASH_VERSINFO[0] >= 5 ))"], check=False).returncode:
            self.skipTest("Bash 5 is required")
        with tempfile.TemporaryDirectory() as home:
            proc, master = self.shell(home)
            self.until(master, PROMPT)
            os.write(master, b"PROMPT_COMMAND=('printf ARRAY_PROMPT')\n")
            self.until(master, PROMPT)
            os.write(master, b'eval "$(kzw sessions bash-hook)"\n')
            self.until(master, PROMPT)
            os.write(master, b'printf "ARRAY_TEST\\n"\n')
            output = self.until(master, PROMPT)
            self.assertIn(b"ARRAY_PROMPT", output)
            with patch.dict(os.environ, {"XDG_STATE_HOME": home}):
                self.assertEqual(session_meta.read(PANE, proc.pid), 'printf "ARRAY_TEST\\n"')
