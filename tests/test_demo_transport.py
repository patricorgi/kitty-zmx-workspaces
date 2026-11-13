"""Real interactive Bash PTY tests for the demo's single-line command transport."""

import errno
import importlib.util
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_transport_runner_test", ROOT / "scripts/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@unittest.skipUnless(shutil.which("bash") and shutil.which("env") and shutil.which("sleep"), "Bash and basic shell tools required")
class DemoTerminalTransportTests(unittest.TestCase):
    def setUp(self):
        self.master, slave = pty.openpty()
        env = {**os.environ, "PS1": "DEMO_READY> ", "PS2": "DEMO_MORE> ", "TERM": "xterm-256color",
               "INPUTRC": "/dev/null", "HISTFILE": "/dev/null"}
        self.process = subprocess.Popen([shutil.which("bash"), "--noprofile", "--norc", "-i"],
                                        stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        self.addCleanup(self.cleanup_terminal)
        self.read_until(b"DEMO_READY> ")
        # Make the same bracketed-paste contract explicit in the isolated shell.
        os.write(self.master, b"bind 'set enable-bracketed-paste on'\r")
        self.read_until(b"DEMO_READY> ")
        self.tools = {"env": shutil.which("env"), "python3": sys.executable,
                      "bash": shutil.which("bash"), "sleep": shutil.which("sleep"),
                      "date": shutil.which("date"), "htop": "/not-run/htop", "yazi": "/not-run/yazi"}

    def cleanup_terminal(self):
        if self.process.poll() is None:
            # This child was created as its own session/process group. Never
            # signal a user's process group or a PID found through name matching.
            if os.getpgid(self.process.pid) == self.process.pid:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        os.close(self.master)

    def read_until(self, needle, timeout=8):
        result = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not select.select([self.master], [], [], .1)[0]:
                continue
            try:
                block = os.read(self.master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not block:
                break
            result += block
            if needle in result:
                return result
        self.fail("isolated terminal did not emit expected output: " + repr(result[-1500:]))

    def launch(self, program, marker):
        command = demo.startup_command(self.tools, "test-token", "matrix", program)
        self.assertNotIn("\n", command)
        self.assertNotIn("\r", command)
        # The two real send-text calls paste a literal single line, then submit
        # Enter outside the bracketed paste. No Python escape decoding is involved.
        os.write(self.master, b"\033[200~" + command.encode() + b"\033[201~")
        os.write(self.master, b"\r")
        output = self.read_until(marker)
        self.assertNotIn(b"DEMO_MORE> ", output)
        return output

    def test_multiline_quotes_unicode_and_environment_reach_the_program_intact(self):
        program = "value=\"quote ' and \\\" and 路径\"\nprintf 'TRANSPORT_OK:%s:%s:%s\\n' \"$KZW_DEMO_TOKEN\" \"$KZW_DEMO_ROLE\" \"$value\""
        marker = "TRANSPORT_OK:test-token:matrix:quote ' and \" and 路径".encode()
        self.launch(program, marker)

    def test_bash_path_with_spaces_quotes_and_unicode_is_not_reparsed_by_readline(self):
        with tempfile.TemporaryDirectory() as directory:
            bash = Path(directory) / "bash ' 路径"
            bash.symlink_to(self.tools["bash"])
            self.tools["bash"] = str(bash)
            self.launch("printf 'ODD_PATH_OK\\n'", b"ODD_PATH_OK")

    def test_actual_fallback_animation_executes_and_restores_terminal_modes(self):
        program = "KZW_DEMO_FRAMES=2\n" + demo.programs(self.tools, "/not-run/nvim")["matrix"]
        output = self.launch(program, b"\033[0m\033[?25h\033[?1049l")
        self.assertIn(b"Frame: 000001", output)


if __name__ == "__main__":
    unittest.main()
