"""Only the reviewed pre-local-tab workspace protocol may cross build fencing."""

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from kzw.remote import (LEGACY_WORKSPACE_BUILDS, LEGACY_WORKSPACE_METHODS, PRE_LOCAL_EXIT_BUILD,
                        PRE_LOCAL_EXIT_METHODS, legacy_workspace_request)
from workspace_fixtures import layout


class BuildCompatibilityTests(unittest.TestCase):
    def test_reviewed_baseline_retains_workspace_operations_without_local_tab_methods(self):
        for build in LEGACY_WORKSPACE_BUILDS:
            info = {"ok": True, "result": {"protocol": 1, "build": build}}
            for method in LEGACY_WORKSPACE_METHODS:
                self.assertTrue(legacy_workspace_request(info, {"method": method, "workspace": "Test"}))
            for method in PRE_LOCAL_EXIT_METHODS:
                self.assertEqual(legacy_workspace_request(info, {"method": method}), build == PRE_LOCAL_EXIT_BUILD)
            for method in ("local.tab.exited", "future.put"):
                self.assertFalse(legacy_workspace_request(info, {"method": method}))

    def test_unknown_versions_protocols_and_malformed_handshakes_still_fail_closed(self):
        build = next(iter(LEGACY_WORKSPACE_BUILDS))
        for info in (
            None, {}, {"ok": False, "result": {"protocol": 1, "build": build}},
            {"ok": True, "result": {"protocol": 2, "build": build}},
            {"ok": True, "result": {"protocol": True, "build": build}},
            {"ok": True, "result": {"protocol": 1, "build": "f" * 64}},
            {"ok": True, "result": {"protocol": 1, "build": []}},
        ):
            self.assertFalse(legacy_workspace_request(info, {"method": "workspace.put"}))
        info = {"ok": True, "result": {"protocol": 1, "build": build}}
        for request in (None, [], {}, {"method": []}):
            self.assertFalse(legacy_workspace_request(info, request))


@unittest.skipUnless(os.environ.get("KZW_RUN_BASELINE_COMPAT_INTEGRATION") == "1",
                     "authentic historical coordinator test is opt-in")
class HistoricalCoordinatorTests(unittest.TestCase):
    def test_new_bridge_finishes_creation_against_unchanged_baseline_server(self):
        repo = Path(__file__).resolve().parents[1]
        source = subprocess.run(["git", "show", "44b1f6f:kzw/remote.py"], cwd=repo,
                                capture_output=True, check=True).stdout
        self.assertIn(hashlib.sha256(source).hexdigest(), LEGACY_WORKSPACE_BUILDS)
        encoded = base64.b64encode(source).decode()
        self.assertIn(hashlib.sha256(encoded.encode()).hexdigest(), LEGACY_WORKSPACE_BUILDS)
        # macOS UNIX sockets need a short path. HOME/PATH/zmx are private stubs;
        # this test never queries a user session or starts a real terminal.
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="kbc-", dir=cache) as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            listing = root / "listed"
            listing.write_text("")
            zmx = bin_dir / "zmx"
            zmx.write_text('#!/bin/sh\n[ "$1 $2" = "list --short" ] || exit 99\ncat "$HOME/listed"\n')
            zmx.chmod(0o700)
            env = dict(os.environ, XDG_STATE_HOME=temp, HOME=temp, ZMX_DIR=str(root / "zmx"),
                       PYTHONPATH=str(repo), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            for key in ("KZW_COORDINATOR_SOURCE", "KZW_LOCAL_COORDINATOR", "ZMX_SESSION", "ZMX_SESSION_PREFIX"):
                env.pop(key, None)
            server_env = dict(env, KZW_COORDINATOR_SOURCE=encoded)
            program = "import base64,os;exec(compile(base64.b64decode(os.environ['KZW_COORDINATOR_SOURCE']),'<baseline>','exec'))"
            with (root / "server.log").open("w+") as log:
                server = subprocess.Popen([sys.executable, "-u", "-c", program, "--serve"], env=server_env,
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=log)

                def bridge(request):
                    result = subprocess.run([sys.executable, "-m", "kzw.remote"], env=env,
                                            input=json.dumps(request) + "\n", text=True,
                                            capture_output=True, check=True, timeout=10)
                    return json.loads(result.stdout)

                try:
                    deadline = time.monotonic() + 5
                    while not (root / "kzw/server.sock").exists() and time.monotonic() < deadline:
                        if server.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        time.sleep(.02)
                    before = bridge({"method": "server.info"})["result"]
                    self.assertEqual(before["pid"], server.pid)
                    self.assertIn(before["build"], LEGACY_WORKSPACE_BUILDS)
                    pane, token = "kzw-" + "a" * 32, "b" * 32
                    self.assertTrue(bridge({"method": "workspace.put", "workspace": "Test", "revision": 0,
                                            "request_id": "create", "snapshot": "# kzw-dormant-workspace\n",
                                            "panes": []})["ok"])
                    self.assertTrue(bridge({"method": "workspace.lease.acquire", "workspace": "Test", "token": token})["ok"])
                    request = {"workspace": "Test", "revision": 1, "lease_token": token}
                    self.assertTrue(bridge({**request, "method": "pane.reserve", "pane": pane, "request_id": "reserve"})["ok"])
                    self.assertTrue(bridge({**request, "method": "workspace.stage", "panes": [pane], "snapshot": layout(pane)})["ok"])
                    listing.write_text(pane + "\n")
                    commit = {**request, "method": "workspace.put", "panes": [pane],
                              "snapshot": layout(pane), "request_id": "commit"}
                    self.assertTrue(bridge(commit)["ok"])
                    state = bridge({"method": "workspace.get", "workspace": "Test"})["result"]
                    self.assertEqual(state["panes"], [pane])
                    self.assertEqual(state["reservations"], [])
                    self.assertIsNone(state["staged"])
                    self.assertEqual(bridge({"method": "server.info"})["result"], before)
                    self.assertFalse(bridge({**commit, "request_id": "stale"})["ok"])
                    unsupported = bridge({"method": "local.tab.get", "owner": "c" * 32})
                    self.assertFalse(unsupported["ok"])
                    self.assertEqual(unsupported["code"], "build_mismatch")
                    self.assertTrue(bridge({"method": "workspace.lease.release", "workspace": "Test", "token": token})["ok"])
                    self.assertTrue(bridge({"method": "server.shutdown"})["ok"])
                    server.wait(timeout=5)
                finally:
                    if server.poll() is None:
                        server.terminate()
                        server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
