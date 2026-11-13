"""Transport reuse is scoped by ownership, and never authorizes session cleanup."""

import io
import os
import concurrent.futures
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
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

    def attachment(self):
        command = ["ssh", *ssh_transport.pane_options("origin", self.env), "-t", "origin", "exec zmx attach " + PANE]
        path = Path(command[6].split("=", 1)[1])
        return command, path

    def socket(self, path):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen(1)
        self.addCleanup(listener.close)
        return listener

    def test_master_preparation_is_command_free_and_never_changes_pane_command(self):
        command, path = self.attachment()
        with patch("kzw.ssh_transport.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            prepared = ssh_transport.prepare_attachment(command, self.env)
        self.assertEqual(prepared[1:7], ["-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ControlPath=" + str(path)])
        self.assertEqual(prepared[7:], command[7:])
        startup, check = [call.args[0] for call in run.call_args_list]
        self.assertIn("-N", startup)
        self.assertIn("-f", startup)
        self.assertIn("BatchMode=yes", startup)
        self.assertEqual(startup[-1], "origin")
        self.assertNotIn(command[-1], startup)
        self.assertEqual(check[1:5], ["-S", str(path), "-O", "check"])
        self.assertEqual((path.with_suffix(".lock").stat().st_mode & 0o777), 0o600)

    def test_master_startup_does_not_enable_interactive_sharing_approval(self):
        command, _ = self.attachment()
        with patch("kzw.ssh_transport.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            ssh_transport.prepare_attachment(command, self.env)
        startup = run.call_args_list[0].args[0]
        self.assertEqual(startup.count("ControlMaster=yes"), 1)
        # OpenSSH's -M toggles an already enabled master into approval (ask) mode.
        self.assertFalse(any(arg.startswith("-") and not arg.startswith("--") and "M" in arg[1:]
                             for arg in startup))
        self.assertFalse(any(arg.lower() in ("controlmaster=ask", "controlmaster=autoask") for arg in startup))

    def test_existing_master_is_checked_without_starting_or_stopping_it(self):
        command, path = self.attachment()
        listener = self.socket(path)
        before = path.stat().st_ino
        with patch("kzw.ssh_transport.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            prepared = ssh_transport.prepare_attachment(command, self.env)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(prepared[6], "ControlPath=" + str(path))
        self.assertEqual(path.stat().st_ino, before)
        self.assertGreaterEqual(listener.fileno(), 0)

    def test_dead_busy_or_suspicious_master_falls_back_without_deleting_socket(self):
        command, path = self.attachment()
        self.socket(path)
        before = path.stat().st_ino
        for failure in (subprocess.CompletedProcess([], 255), OSError("offline"), subprocess.TimeoutExpired("ssh", 3)):
            with self.subTest(failure=failure), patch("kzw.ssh_transport.subprocess.run") as run:
                if isinstance(failure, Exception):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                prepared = ssh_transport.prepare_attachment(command, self.env)
                self.assertEqual(prepared[6], "ControlPath=none")
                self.assertEqual(prepared[-1], command[-1])
                self.assertEqual(path.stat().st_ino, before)
                self.assertEqual(run.call_count, 1)
        path.unlink()  # Only this test's disposable listener path.
        path.write_text("unrelated file")
        with patch("kzw.ssh_transport.subprocess.run") as run:
            self.assertEqual(ssh_transport.prepare_attachment(command, self.env)[6], "ControlPath=none")
        run.assert_not_called()
        self.assertEqual(path.read_text(), "unrelated file")

    def test_failed_or_timed_out_master_start_uses_direct_connection(self):
        command, path = self.attachment()
        for failure in (subprocess.CompletedProcess([], 255), OSError("unavailable"), subprocess.TimeoutExpired("ssh", 15)):
            with self.subTest(failure=failure), patch("kzw.ssh_transport.subprocess.run") as run:
                if isinstance(failure, Exception):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                prepared = ssh_transport.prepare_attachment(command, self.env)
                self.assertEqual(prepared[6], "ControlPath=none")
                self.assertEqual(run.call_count, 1)
                self.assertFalse(path.exists())

    def test_parallel_first_attachments_start_only_one_master(self):
        command, path = self.attachment()
        calls = []
        lock = threading.Lock()
        def run(cmd, **kwargs):
            with lock:
                calls.append(cmd)
                if "-N" in cmd:
                    self.socket(path)
            return subprocess.CompletedProcess(cmd, 0)
        with patch("kzw.ssh_transport.subprocess.run", side_effect=run), concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: ssh_transport.prepare_attachment(command, self.env), range(6)))
        self.assertEqual(sum("-N" in cmd for cmd in calls), 1)
        self.assertTrue(all(cmd[6] == "ControlPath=" + str(path) for cmd in results))
        self.assertTrue(all(cmd[-1] == command[-1] for cmd in results))

    def test_setup_lock_timeout_uses_direct_connection_and_unsafe_lock_is_rejected(self):
        command, path = self.attachment()
        with patch("kzw.ssh_transport.fcntl.flock", side_effect=BlockingIOError), \
             patch("kzw.ssh_transport.time.monotonic", side_effect=[0, 16]), patch("kzw.ssh_transport.subprocess.run") as run:
            self.assertEqual(ssh_transport.prepare_attachment(command, self.env)[6], "ControlPath=none")
        run.assert_not_called()
        lock = path.with_suffix(".lock")
        lock.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "unsafe SSH"):
            ssh_transport.prepare_attachment(command, self.env)

    def test_successful_builtin_fallback_hides_only_mux_errors_but_failed_fallback_reports_them(self):
        command, _ = self.attachment()
        prepared = ["ssh", "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ControlPath=/test", *command[7:]]
        noise = "mux_client_request_session: read from master failed: Broken pipe\n"
        warning = "Warning: a genuine SSH warning\n"
        for status in (0, 255):
            def run(cmd, **kwargs):
                os.write(kwargs["stderr"], (noise + warning).encode())
                return subprocess.CompletedProcess(cmd, status)
            with self.subTest(status=status), patch("kzw.ssh_transport.prepare_attachment", return_value=prepared), \
                 patch("kzw.pane.subprocess.run", side_effect=run), patch("sys.stderr", new_callable=io.StringIO) as stderr:
                result = pane.run_attachment(command, self.env, True)
            self.assertEqual(result.returncode, status)
            self.assertIn(warning, stderr.getvalue())
            self.assertEqual(noise in stderr.getvalue(), status != 0)

    def test_normal_failure_is_not_retried_or_hidden_and_debug_keeps_raw_stderr(self):
        command, _ = self.attachment()
        for debug in (False, True):
            env = {**self.env, "KZW_DEBUG": "1" if debug else "0"}
            failure = "Permission denied (publickey).\n"
            def run(cmd, **kwargs):
                if "stderr" in kwargs:
                    os.write(kwargs["stderr"], failure.encode())
                return subprocess.CompletedProcess(cmd, 255)
            with self.subTest(debug=debug), patch("kzw.ssh_transport.prepare_attachment", return_value=command), \
                 patch("kzw.pane.subprocess.run", side_effect=run) as process, patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(pane.run_attachment(command, env, True).returncode, 255)
            process.assert_called_once()
            if debug:
                self.assertNotIn("stderr", process.call_args.kwargs)
            else:
                self.assertIn(failure, stderr.getvalue())
