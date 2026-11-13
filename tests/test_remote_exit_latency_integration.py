"""Opt-in remote GUI exits: timing plus preservation after an SSH client dies."""

import json
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest
from unittest.mock import patch

from kzw import cli, remote_client
import test_cwd_integration as cwd_tests


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1" and
                     os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "remote Kitty exits are opt-in")
class RemoteExitLatencyIntegrationTests(unittest.TestCase):
    def test_remote_disconnect_then_split_tab_and_final_exit(self):
        fixture = cwd_tests.CwdIntegrationTests("test_remote_tabs_and_splits_inherit_selected_shell_cwd")
        fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups(), "remote exit fixture cleanup failed"))
        kit, host = fixture.kitty, os.environ["KZW_INTEGRATION_HOST"]
        unrelated = json.loads(kit.raw("ls"))[0]
        result = kit.kzw("create", "Test", "--attach", "--host", host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = fixture.windows()[0]
        with patch.dict(os.environ, kit.env):
            directory = cli.state_dir("Test", host)
        for kind in ("right", "tab"):
            for _ in range(10):
                result = kit.kzw("_managed-create", "Test", kind, str(first["id"]), first["user_vars"]["kzw_pane"],
                                 "--host", host, "--origin", host)
                if result.returncode == 0:
                    break
                self.assertEqual(result.stderr.strip(), "kzw: layout changed while taking snapshot")
                self.assertFalse((directory / "pending-create.json").exists())
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        original = {w["id"]: w for w in fixture.windows()}
        source, split, tab = sorted(original)
        owned = {w["user_vars"]["kzw_pane"] for w in original.values()}

        def remote_identities():
            rows = [dict(field.split("=", 1) for field in line.strip().split("\t"))
                    for line in cli.zmx_command(host, "list").splitlines()]
            return {row["name"]: (row["pid"], row["created"]) for row in rows if row["name"] in owned}

        before = remote_identities()
        self.assertEqual(set(before), owned)
        processes = subprocess.check_output(["ps", "-axo", "pid=,ppid=,comm="], text=True)
        clients = [int(fields[0]) for line in processes.splitlines() if len(fields := line.split()) >= 3
                   and int(fields[1]) == original[split]["pid"] and Path(fields[2]).name == "ssh"]
        self.assertEqual(len(clients), 1)
        os.kill(clients[0], signal.SIGKILL)
        fixture.wait(lambda: next(w for w in fixture.windows() if w["id"] == split)["user_vars"].get("kzw_attach_ended") == "1")
        self.assertEqual(set(json.loads((directory / "panes.json").read_text())), owned)
        self.assertEqual(remote_identities(), before)
        fixture.wait(lambda: next(w for w in fixture.windows() if w["id"] == split)["user_vars"].get("kzw_attach_ended") == "0")

        timings = []
        for wid, text in ((split, "\x04"), (tab, "exit 7\r"), (source, "\x04")):
            token = "KZW_EXIT_READY_" + str(wid)
            last_send = [0.0]

            def ready():
                if "\n" + token + "\n" in kit.raw("get-text", "--match", "id:" + str(wid)):
                    return True
                if time.monotonic() - last_send[0] > 2:
                    kit.raw("send-text", "--match", "id:" + str(wid), "echo " + token + "\r")
                    last_send[0] = time.monotonic()
                return False

            fixture.wait(ready)
            start = time.monotonic()
            kit.raw("send-text", "--match", "id:" + str(wid), text)
            fixture.wait(lambda: wid not in {w["id"] for w in fixture.windows()})
            timings.append(round(time.monotonic() - start, 3))
            fixture.wait(lambda: not (directory / "pending-exit.json").exists())
            for w in fixture.windows():
                self.assertEqual(w["pid"], original[w["id"]]["pid"])
        fixture.wait(lambda: not (directory / "view.json").exists())
        self.assertEqual(remote_identities(), {})
        self.assertEqual((directory / "workspace.kitty-session").read_text(), "# kzw-empty-workspace\n")
        with patch.dict(os.environ, kit.env):
            self.assertEqual(remote_client.call(host, {"method": "cleanup.list", "workspace": "Test"})["panes"], [])
        remaining = json.loads(kit.raw("ls"))
        self.assertEqual([w["id"] for w in remaining], [unrelated["id"]])
        self.assertEqual(remaining[0]["tabs"][0]["windows"][0]["pid"], unrelated["tabs"][0]["windows"][0]["pid"])
        print("Remote split/tab/final exit latency (s):", timings)
