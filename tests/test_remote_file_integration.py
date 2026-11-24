"""Save an actual OSC 8 link from a restored managed pane through Kitty's UI."""

import base64
import fcntl
import json
import os
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

import test_cwd_integration as cwd_tests
from kzw import cli


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1" and
                     os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "remote Kitty test is opt-in")
class RemoteFileIntegrationTests(unittest.TestCase):
    def test_download_from_reattached_pane(self):
        fixture = cwd_tests.CwdIntegrationTests("test_remote_tabs_and_splits_inherit_selected_shell_cwd")
        with tempfile.TemporaryDirectory() as config, patch.dict(os.environ, {
                "XDG_CONFIG_HOME": config, "KITTY_CACHE_DIRECTORY": config}):
            fixture.setUp()
            try:
                self.check_download(fixture)
            finally:
                self.assertTrue(fixture.doCleanups())

    def check_download(self, fixture):
        kit, host = fixture.kitty, os.environ["KZW_INTEGRATION_HOST"]
        payload = b"kzw remote file\n\x00\xff"
        path = fixture.remote_root + "/test file.txt"
        create = subprocess.run(["ssh", "-o", "BatchMode=yes", host, "cat > " + shlex.quote(path) + " && uname -n"],
                                input=payload, capture_output=True, check=True, timeout=20)
        hostname = create.stdout.decode().strip()
        def command(*args):
            result = kit.kzw(*args, "Test", "--host", host)
            self.assertEqual(result.returncode, 0, result.stderr)
        command("create", "--attach")
        command("detach")
        with patch.dict(os.environ, kit.env):
            directory = cli.state_dir("Test", host)

        def detached():
            with (directory / "lock").open("r") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return not (directory / "lease.json").exists()

        fixture.wait(detached)
        command("attach")
        window, = fixture.wait(fixture.windows)
        match = f"id:{window['id']}"
        uri = f"file://{hostname}{quote(path)}"
        last = [0.]

        def listed():
            if uri in kit.raw("get-text", "--ansi", "--match", match):
                return True
            if time.monotonic() - last[0] > 2:
                kit.raw("send-text", "--match", match, "command ls -d --hyperlink=auto " + shlex.quote(path) + "\r")
                last[0] = time.monotonic()

        fixture.wait(listed)
        probe = kit.tmp / "activate_link.py"
        probe.write_text(
            "from kittens.tui.handler import result_handler\n"
            "def main(args): pass\n"
            "@result_handler(no_ui=True)\n"
            "def handle_result(args, answer, target, boss):\n"
            " w=boss.window_id_map[target]\n"
            " if 'handle_remote_file' not in vars(w): return 'pending'\n"
            " i, = [i for text,i in w.screen.hyperlinks_as_set() if text == ':' + args[1]]\n"
            " w.open_url(args[1], i)\n"
            " return 'opened'\n")
        def windows():
            return [w for ow in json.loads(kit.raw("ls")) for tab in ow["tabs"] for w in tab["windows"]]

        before = {w["id"] for w in windows()}
        try:
            fixture.wait(lambda: kit.raw("kitten", "--match", match, str(probe), uri).strip() == "opened")
            def prompt(text):
                for w in windows():
                    if w["id"] not in before and text in kit.raw("get-text", "--match", f"id:{w['id']}"):
                        return f"id:{w['id']}"
            overlay = fixture.wait(lambda: prompt("Save the file"))
            kit.raw("send-key", "--match", overlay, "s")
            fixture.wait(lambda: prompt("Where do you want to save"))
            dest = kit.tmp / "saved file.txt"
            kit.raw("send-text", "--match", overlay, str(dest))
            kit.raw("send-key", "--match", overlay, "enter")
            fixture.wait(lambda: dest.is_file() and dest.read_bytes() == payload)
            fixture.wait(lambda: {w["id"] for w in windows()} == before)
            # A Yazi-style request reaches the same chooser without activating a link.
            request = base64.b64encode(json.dumps([hostname, path]).encode()).decode()
            packet = base64.b64encode(f"\033]1337;SetUserVar=kzw_file_menu={request}\007".encode()).decode()
            kit.raw("send-text", "--match", match, f"echo {packet} | base64 -d\r")
            overlay = fixture.wait(lambda: prompt("Save the file"))
            menu = kit.raw("get-text", "--match", overlay)
            for item in ("Edit the file", "Open the file", "Cancel", "test file.txt", hostname):
                self.assertIn(item, menu)
            kit.raw("send-key", "--match", overlay, "c")
            fixture.wait(lambda: {w["id"] for w in windows()} == before)
        finally:
            for w in windows():
                if w["id"] not in before:
                    kit.raw("close-window", "--match", f"id:{w['id']}")
