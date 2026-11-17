"""Opt-in SSH-client loss/retry in a hidden, isolated Test workspace.

This injects a local attachment failure, not an Internet outage. Remote programs
and other SSH clients must retain their original process/session identities.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest

import test_cwd_integration as cwd_tests
from test_shared_instance_integration import REPO


@unittest.skipUnless(os.environ.get("KZW_RUN_CONNECTION_INTEGRATION") == "1", "SSH reconnect test is opt-in")
class ConnectionIntegrationTests(unittest.TestCase):
    def test_ssh_attachment_retry_preserves_sessions_layout_and_healthy_pane(self):
        host = os.environ["KZW_INTEGRATION_HOST"]
        fixture = cwd_tests.CwdIntegrationTests("test_remote_tabs_and_splits_inherit_selected_shell_cwd")
        fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups(), "connection fixture cleanup failed"))
        kit = fixture.kitty
        unrelated = json.loads(kit.raw("ls"))[0]
        result = kit.kzw("create", "Test", "--attach", "--host", host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = fixture.wait(lambda: fixture.windows())[0]
        result = kit.kzw("_managed-create", "Test", "tab", str(first["id"]), first["user_vars"]["kzw_pane"],
                         "--host", host, "--origin", host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fixture.wait(lambda: len(fixture.windows()) == 2 and all(
            "kzw_connection" in w["user_vars"] and not w["user_vars"]["kzw_connection"] for w in fixture.windows()))
        windows = fixture.windows()
        first, healthy = windows
        os_window = next(ow for ow in json.loads(kit.raw("ls")) if any(
            w["id"] == first["id"] for tab in ow["tabs"] for w in tab["windows"]))
        probe = str(REPO / "tests/kitty_metrics_probe.py")
        renderer = Path(os.environ.get("KZW_TEST_TAB_BAR", Path.home() / ".config/kitty/tab_bar.py"))
        kit.raw("kitten", probe, str(os_window["id"]), "install", str(renderer))
        keys = str(REPO / "tests/kitty_shortcuts_probe.py")
        before_keys = json.loads(kit.raw("kitten", keys))

        def inspect():
            return json.loads(kit.raw("kitten", probe, str(os_window["id"])))

        fixture.wait(lambda: inspect()["record"] and not inspect()["record"].get("connection"))

        def ssh_child(window):
            table = subprocess.check_output(["ps", "-axo", "pid=,ppid=,uid=,command="], text=True)
            matches = []
            for line in table.splitlines():
                fields = line.split(None, 3)
                if len(fields) != 4:
                    continue
                pid, parent, uid, command = fields
                if (int(parent) == window["pid"] and int(uid) == os.getuid() and
                        " -t " + host + " " in command and "exec zmx attach " + window["user_vars"]["kzw_pane"] in command):
                    matches.append(int(pid))
            self.assertEqual(len(matches), 1, "cannot prove exact disposable SSH child")
            return matches[0]

        def identities():
            output = subprocess.check_output(["ssh", "-T", "-o", "BatchMode=yes", host,
                                               '"$HOME/.local/bin/zmx" list'], text=True, timeout=15)
            result = {}
            for line in output.splitlines():
                fields = dict(part.split("=", 1) for part in line.strip().split("\t") if "=" in part)
                if fields.get("name") in fixture.owned:
                    result[fields["name"]] = (fields["pid"], fields["created"])
            self.assertEqual(set(result), fixture.owned)
            return result

        before_sessions = identities()
        healthy_ssh = ssh_child(healthy)
        before_pids = {w["id"]: w["pid"] for w in windows}
        before_layout = [(tab["id"], tab["layout"], tab["layout_state"], [w["id"] for w in tab["windows"]])
                         for tab in os_window["tabs"]]
        # Revalidate immediately before signalling only our own SSH client.
        target = ssh_child(first)
        self.assertIn(first["user_vars"]["kzw_pane"], fixture.owned)
        os.kill(target, signal.SIGTERM)
        fixture.wait(lambda: any(w["id"] == first["id"] and w["user_vars"].get("kzw_connection") == "reconnecting"
                                 for w in fixture.windows()))
        def reconnecting():
            value = inspect()
            return value if "Reconnecting…" in value["line"] else None

        shown = fixture.wait(reconnecting)
        self.assertNotIn("⌘", shown["line"])
        self.assertNotIn("%", shown["line"])
        self.assertEqual(shown["fg"][shown["line"].index("Reconnecting…")], 0xf9e2af)
        requested = time.monotonic()
        kit.raw("kitten", "--match", "id:" + str(first["id"]), str(REPO / "kzw/retry_kitten.py"))
        fixture.wait(lambda: all("kzw_connection" in w["user_vars"] and not w["user_vars"]["kzw_connection"]
                                 for w in fixture.windows()))
        self.assertLess(time.monotonic() - requested, 5, "manual retry did not beat normal backoff")
        self.assertEqual(ssh_child(healthy), healthy_ssh)
        self.assertNotEqual(ssh_child(first), target)
        self.assertEqual(identities(), before_sessions)
        self.assertEqual({w["id"]: w["pid"] for w in fixture.windows()}, before_pids)
        after = next(ow for ow in json.loads(kit.raw("ls")) if ow["id"] == os_window["id"])
        self.assertEqual([(tab["id"], tab["layout"], tab["layout_state"], [w["id"] for w in tab["windows"]])
                          for tab in after["tabs"]], before_layout)
        after_keys = json.loads(kit.raw("kitten", keys))
        self.assertEqual(after_keys["global"], before_keys["global"])
        self.assertEqual(after_keys["modes"], before_keys["modes"])
        remaining = next(ow for ow in json.loads(kit.raw("ls")) if ow["id"] == unrelated["id"])
        self.assertEqual(remaining["tabs"][0]["windows"][0]["pid"], unrelated["tabs"][0]["windows"][0]["pid"])
        endpoints = [Path(json.loads(w["user_vars"]["kzw_retry_endpoint"])["path"]) for w in fixture.windows()]
        endpoints.append(Path(inspect()["record"]["retry"]["path"]))
        result = kit.kzw("detach", "Test", "--host", host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 10
        while any(path.parent.exists() for path in endpoints) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(any(path.parent.exists() for path in endpoints), "retry endpoints remained after normal detach")
        print("SSH retry preserved both remote session identities, healthy SSH child, layout and native shortcuts")
