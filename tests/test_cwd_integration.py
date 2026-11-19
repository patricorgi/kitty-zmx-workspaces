"""Opt-in cwd inheritance through actual managed tab/split creation."""

import fcntl
import json
import os
import shlex
import subprocess
import time
import unittest
import uuid
from unittest.mock import patch

from kzw import cli, remote_client, shell_cwd
import test_shared_instance_integration as shared


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "hidden Kitty test is opt-in")
class CwdIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.owned = set()
        self.remote_root = ""
        if self._testMethodName.startswith("test_remote"):
            host = os.environ["KZW_INTEGRATION_HOST"]
            self.remote_baseline = set(cli.zmx_command(host, "list", "--short").splitlines())
            self.remote_root = subprocess.check_output(
                ["ssh", "-o", "BatchMode=yes", host,
                 'mkdir -p "$HOME/.cache" && mktemp -d "$HOME/.cache/kzw-cwd-it-XXXXXXXX"'],
                text=True, timeout=20).strip()
            self.assertTrue(self.remote_root.startswith("/") and "/.cache/kzw-cwd-it-" in self.remote_root)
        self.kitty = shared.SharedInstanceIntegrationTests("test_attach_in_shared_instance_and_detach_only_workspace")
        with patch.dict(os.environ, {"KZW_REMOTE_STATE_HOME": self.remote_root}):
            self.kitty.setUp()
        self.kitty.baseline = self.kitty.sessions()
        self.addCleanup(self.kitty.tearDown)
        if self.remote_root:
            self.addCleanup(self.cleanup_remote, host, self.remote_root)

    def windows(self):
        result = [w for ow in json.loads(self.kitty.raw("ls")) for tab in ow["tabs"] for w in tab["windows"]
                  if w.get("user_vars", {}).get("kzw_workspace") == "Test" and w["user_vars"].get("kzw_pane")]
        self.owned.update(w["user_vars"]["kzw_pane"] for w in result)
        return result

    def wait(self, predicate):
        deadline = time.monotonic() + 45
        last = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                last = exc
            time.sleep(0.1)
        screens = {w["id"]: self.kitty.raw("get-text", "--match", "id:" + str(w["id"])) for w in self.windows()}
        self.fail("managed cwd inheritance timed out: " + str(last) + "\n" + repr(screens))

    def check_inheritance(self, host, paths):
        kit = self.kitty
        origin = ["--host", host] if host else []
        result = kit.kzw("create", "Test", "--attach", *origin)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = self.wait(lambda: self.windows())[0]
        with patch.dict(os.environ, kit.env):
            directory = cli.state_dir("Test", host)

        def cwd(window):
            with patch.dict(os.environ, kit.env):
                return shell_cwd.from_session(window["user_vars"]["kzw_pane"], host)

        def change(window, path):
            self.wait(lambda: cwd(window))
            # SSH/zmx can flush input while attaching; retry only this disposable
            # shell's idempotent cd until its origin cwd confirms delivery.
            last_send = [0.0]
            def changed():
                if cwd(window) == path:
                    return True
                if time.monotonic() - last_send[0] > 2:
                    kit.raw("send-text", "--match", "id:" + str(window["id"]), "command cd -- " + shlex.quote(path) + "\n")
                    last_send[0] = time.monotonic()
                return False
            self.wait(changed)

        def create(source, kind, count):
            kit.raw("kitten", "--match", "id:" + str(source["id"]), str(shared.REPO / "kzw/create_kitten.py"), kind)
            self.wait(lambda: len(self.windows()) == count and not (directory / "pending-create.json").exists())
            return max(self.windows(), key=lambda w: w["id"])

        change(first, paths[0])
        second = create(first, "right", 2)
        self.wait(lambda: cwd(second) == paths[0])
        change(second, paths[1])
        third = create(second, "tab", 3)
        self.wait(lambda: cwd(third) == paths[1])
        self.assertEqual(cwd(first), paths[0])
        self.assertEqual(cwd(second), paths[1])
        self.assertEqual(next(w for w in self.windows() if w["id"] == first["id"])["pid"], first["pid"])
        self.assertEqual(len(self.owned), 3)

    def test_local_tabs_and_splits_inherit_selected_shell_cwd(self):
        paths = [self.kitty.tmp / "first directory", self.kitty.tmp / "second's directory"]
        for path in paths:
            path.mkdir()
        self.check_inheritance("", [str(p.resolve()) for p in paths])

    @unittest.skipUnless(os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "SSH cwd test is opt-in")
    def test_remote_tabs_and_splits_inherit_selected_shell_cwd(self):
        host = os.environ["KZW_INTEGRATION_HOST"]
        root = self.remote_root
        paths = [root + "/first directory", root + "/second's directory"]
        subprocess.run(["ssh", "-o", "BatchMode=yes", host, shlex.join(["mkdir", "-p", "--", *paths])], check=True, timeout=20)
        self.check_inheritance(host, paths)

    @unittest.skipUnless(os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "SSH RPC test is opt-in")
    def test_remote_attachment_reuses_foreground_bridge_for_status(self):
        host = os.environ["KZW_INTEGRATION_HOST"]
        result = self.kitty.kzw("create", "Test", "--attach", "--host", host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with patch.dict(os.environ, self.kitty.env):
            directory = cli.state_dir("Test", host)
        self.wait(self.windows)
        notice = directory / "remote-status.changed"
        self.wait(notice.is_file)
        pid = json.loads((directory / "local-tab-capability.json").read_text())["pid"]
        processes = subprocess.check_output(["ps", "-axo", "ppid=,command="], text=True)
        bridges = [command for line in processes.splitlines() if line.strip()
                   for parent, command in [line.split(None, 1)]
                   if parent == str(pid) and command.startswith("ssh -T ") and self.remote_root in command]
        self.assertEqual(len(bridges), 2)

    @unittest.skipUnless(os.environ.get("KZW_RUN_ATTACH_BENCHMARK") == "1", "attach benchmark is opt-in")
    def test_remote_cold_attach_to_shell_input(self):
        host, kit = os.environ["KZW_INTEGRATION_HOST"], self.kitty
        kit.env["KZW_SSH_KITTEN"] = "0"
        result = kit.kzw("create", "Test", "--attach", "--host", host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        original = self.wait(self.windows)[0]["user_vars"]["kzw_pane"]
        with patch.dict(os.environ, kit.env):
            directory = cli.state_dir("Test", host)

        def detached():
            with (directory / "lock").open("r") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return not (directory / "lease.json").exists()

        for attempt, mode in enumerate(("0", "1", "1", "0", "0", "1")):
            result = kit.kzw("detach", "Test", "--host", host)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.wait(detached)
            # A fresh private socket path makes this terminal connection cold
            # without stopping a master that may still have clients.
            cache = kit.tmp / str(attempt)
            cache.mkdir(mode=0o700)
            kit.env["XDG_CACHE_HOME"] = str(cache)
            kit.env["KZW_ATTACH_REUSE_SSH"] = mode
            started = time.monotonic()
            result = kit.kzw("attach", "Test", "--host", host)
            launch = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pane = self.wait(self.windows)[0]
            self.assertEqual(pane["user_vars"]["kzw_pane"], original)
            match = f"id:{pane['id']}"
            token = uuid.uuid4().hex
            last_send = 0

            def command_ran():
                nonlocal last_send
                if token in kit.raw("get-text", "--match", match):
                    return True
                if time.monotonic() - last_send >= 0.3:
                    last_send = time.monotonic()
                    kit.raw("send-text", "--match", match,
                            f"printf '%s%s' '{token[:16]}' '{token[16:]}'\r")
                return False

            self.wait(command_ran)
            print(f"rpc reuse={mode}: {launch:.2f}s to CLI return, "
                  f"{time.monotonic() - started:.2f}s to shell input", flush=True)

    @unittest.skipUnless(os.environ.get("KZW_RUN_SSH_KITTEN_INTEGRATION") == "1", "SSH kitten benchmark is opt-in")
    def test_remote_ssh_kitten_managed_shell_readiness(self):
        host = os.environ["KZW_INTEGRATION_HOST"]
        kit = self.kitty
        for mode in ("0", "1"):
            kit.env["KZW_SSH_KITTEN"] = mode
            result = kit.kzw("create" if mode == "0" else "attach", "Test",
                             *(["--attach"] if mode == "0" else []), "--host", host)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            anchor = min(self.wait(self.windows), key=lambda w: w["id"])
            if mode == "1":
                self.assertEqual(anchor["user_vars"]["kzw_pane"], original)
            original = anchor["user_vars"]["kzw_pane"]

            def ready(window):
                match = "id:" + str(window["id"])
                token = uuid.uuid4().hex
                last_send = 0

                def command_ran():
                    nonlocal last_send
                    if token in kit.raw("get-text", "--match", match):
                        return True
                    if time.monotonic() - last_send >= 0.3:
                        last_send = time.monotonic()
                        kit.raw("send-text", "--match", match,
                                f"printf '%s%s' '{token[:16]}' '{token[16:]}'\r")
                    return False

                self.wait(command_ran)
                if mode == "1":
                    def using_kitten():
                        processes = subprocess.check_output(["ps", "-axo", "ppid=,command="], text=True)
                        return any(line.lstrip().startswith(str(window["pid"]) + " ")
                                   and ("+kitten ssh" in line or "kitten ssh" in line)
                                   for line in processes.splitlines())
                    self.assertTrue(using_kitten(), "pane became ready without the SSH kitten")

            ready(anchor)
            for kind in ("tab", "split"):
                before = {w["id"] for w in self.windows()}
                started = time.monotonic()
                kit.raw("kitten", "--match", "id:" + str(anchor["id"]),
                        str(shared.REPO / "kzw/create_kitten.py"), kind)
                new = self.wait(lambda: next((w for w in self.windows() if w["id"] not in before), None))
                ready(new)
                print(f"kitten={mode}, {kind}: {time.monotonic() - started:.2f}s to shell command", flush=True)
            result = kit.kzw("detach", "Test", "--host", host)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def cleanup_remote(self, host, root):
        self.windows()
        # Reservations also cover a failed launch before a pane becomes visible.
        inventory = ("import json,os,sqlite3,sys; p=sys.argv[1]+'/kzw/remote.sqlite3'; ids=set(); "
                     "db=sqlite3.connect('file:'+p+'?mode=ro',uri=True) if os.path.isfile(p) else None; "
                     "[ids.update(json.loads(row[0])) for table in ('workspaces','staged_layouts') "
                     "for row in (db.execute('SELECT panes FROM '+table) if db else [])]; "
                     "[ids.add(row[0]) for row in (db.execute('SELECT pane FROM reservations') if db else [])]; "
                     "print(json.dumps(sorted(ids)))")
        self.owned.update(json.loads(subprocess.check_output(
            ["ssh", "-o", "BatchMode=yes", host, shlex.join(["python3", "-c", inventory, root])], text=True, timeout=20)))
        self.assertFalse(self.owned & self.remote_baseline)
        with patch.dict(os.environ, self.kitty.env):
            directory = cli.state_dir("Test", host)
        if (directory / "view.json").exists():
            result = self.kitty.kzw("detach", "Test", "--host", host)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        live = set(cli.zmx_command(host, "list", "--short").splitlines())
        for pane in self.owned & live:
            cli.zmx_command(host, "kill", pane)
        self.assertFalse(self.owned & set(cli.zmx_command(host, "list", "--short").splitlines()))
        with patch.dict(os.environ, self.kitty.env):
            for _ in range(30):
                try:
                    remote_client.call(host, {"method": "server.shutdown"})
                    break
                except (OSError, ValueError, subprocess.SubprocessError):
                    time.sleep(0.1)
            else:
                self.fail("isolated remote coordinator remains at " + root)
        cleanup = ("import os,sys,time,shutil; p=sys.argv[1]; "
                   "assert os.path.dirname(p)==os.path.expanduser('~/.cache') and os.path.basename(p).startswith('kzw-cwd-it-'); "
                   "s=os.path.join(p,'kzw','server.sock'); "
                   "[(time.sleep(.1)) for _ in range(50) if os.path.exists(s)]; "
                   "assert not os.path.exists(s); shutil.rmtree(p)")
        subprocess.run(["ssh", "-o", "BatchMode=yes", host, shlex.join(["python3", "-c", cleanup, root])], check=True, timeout=20)
