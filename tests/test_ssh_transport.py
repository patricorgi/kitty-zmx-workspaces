"""Transport reuse is scoped by ownership, and never authorizes session cleanup."""

import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kzw import pane, ssh_transport

PANE = "kzw-" + "a" * 32


class SSHTransportTests(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kt-", dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.env = {"XDG_CACHE_HOME": self.temp.name, "KZW_WORKSPACE": "Test", "KZW_WORKSPACE_ORIGIN": "origin"}

    def test_socket_reuse_and_isolation_use_workspace_origin_and_target_host(self):
        expected = ssh_transport.pane_options("origin", self.env)
        self.assertEqual(expected[:4], ["-o", "ControlMaster=auto", "-o", "ControlPersist=60"])
        path = Path(expected[-1].split("=", 1)[1])
        self.assertEqual(path.parent, Path(self.temp.name) / "kzw/ssh")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(path.exists())  # choosing options never creates a master
        cases = (("same", "origin", {}, True),
                 ("other host", "other", {}, False),
                 ("other workspace", "origin", {"KZW_WORKSPACE": "Other"}, False),
                 ("other origin", "origin", {"KZW_WORKSPACE_ORIGIN": "another"}, False),
                 ("unrelated context", "origin", {"KZW_DEBUG": "1", "TERM": "other"}, True))
        for name, host, changes, same in cases:
            with self.subTest(case=name):
                self.assertEqual(ssh_transport.pane_options(host, {**self.env, **changes}) == expected, same)
        with patch("kzw.ssh_transport.Path.mkdir") as mkdir:
            self.assertEqual(ssh_transport.pane_options("", self.env), [])
            self.assertEqual(ssh_transport.pane_options("origin", {}), [])
            mkdir.assert_not_called()

    def test_unsafe_socket_roots_and_byte_length_limits_fail_closed(self):
        root = Path(self.temp.name) / "kzw/ssh"
        root.parent.mkdir()
        baseline = Path(ssh_transport.pane_options("origin", self.env)[-1].split("=", 1)[1])
        root.rmdir()
        target = Path(self.temp.name) / "target"
        target.mkdir(mode=0o700)
        for kind in ("symlink", "file", "public", "foreign-owner", "too-long"):
            with self.subTest(case=kind):
                env = dict(self.env)
                if kind == "symlink":
                    root.symlink_to(target, target_is_directory=True)
                elif kind == "file":
                    root.write_text("keep")
                elif kind == "public":
                    root.mkdir(mode=0o755)
                    root.chmod(0o755)
                elif kind == "too-long":
                    # Fit below 100 characters while exceeding the byte limit.
                    component = "é" * ((100 - len(str(baseline))) // 2)
                    env["XDG_CACHE_HOME"] += "/" + component
                    candidate = Path(env["XDG_CACHE_HOME"]) / "kzw/ssh" / baseline.name
                    self.assertLess(len(str(candidate)), 100)
                    self.assertGreaterEqual(len(os.fsencode(candidate)), 100)
                else:
                    root.mkdir(mode=0o700)
                owner = os.getuid() + 1 if kind == "foreign-owner" else os.getuid()
                with patch("kzw.ssh_transport.os.getuid", return_value=owner), self.assertRaises((ValueError, FileExistsError)):
                    ssh_transport.pane_options("origin", env)
                self.assertTrue(target.is_dir())
                if root.is_symlink() or root.is_file():
                    root.unlink()
                elif root.exists():
                    root.rmdir()

    def test_remote_pane_commands_reuse_options_for_creation_and_existing_attachment(self):
        commands = []
        for existing in (False, True):
            with self.subTest(existing=existing), patch.dict(os.environ, self.env, clear=True), \
                 patch("sys.argv", ["kzw-pane", "--pane", PANE, "--host", "origin"] + (["--existing"] if existing else [])), \
                 patch("sys.stdout", new_callable=io.StringIO), \
                 patch("kzw.pane.verify_workspace_lease"), patch("kzw.pane.report_attachment_ended") as ended, \
                 patch("kzw.pane.run_attachment", return_value=subprocess.CompletedProcess([], 0)) as attach:
                pane.main()
            command, env, remote = attach.call_args.args
            commands.append(command)
            self.assertTrue(remote)
            self.assertEqual(command[:7], ["ssh", *ssh_transport.pane_options("origin", self.env)])
            self.assertEqual(command[7:9], ["-t", "origin"])
            self.assertEqual("zmx list --short" in command[-1], existing)
            self.assertIn("exec zmx attach " + PANE, command[-1])
            self.assertNotIn("ZMX_SESSION_PREFIX", env)
            ended.assert_called_once_with(retry=False)
        self.assertEqual(commands[0][1:7], commands[1][1:7])

    def test_diagnostic_filter_only_hides_known_fallback_messages(self):
        cases = {
            "mux_client_request_session: session request failed: Session open refused by peer\n": False,
            "ControlSocket /some/path already exists, disabling multiplexing\n": False,
            "mux_client_request_session: read from master failed: Broken pipe\n": True,
            "ControlSocket /some/path: Permission denied\n": True,
            "Permission denied (publickey).\n": True,
            "ssh: Could not resolve hostname origin\n": True,
            "Connection reset by peer\n": True,
        }
        for message, visible in cases.items():
            with self.subTest(message=message):
                self.assertEqual(pane.ssh_diagnostic_visible(message), visible)
        for remote, debug in ((False, "0"), (False, "1"), (True, "1")):
            with self.subTest(remote=remote, debug=debug), patch("kzw.pane.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 255)) as run:
                result = pane.run_attachment(["program"], {"KZW_DEBUG": debug}, remote)
                self.assertEqual(result.returncode, 255)
                self.assertNotIn("stderr", run.call_args.kwargs)
