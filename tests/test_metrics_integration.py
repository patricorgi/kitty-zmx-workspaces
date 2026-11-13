"""Opt-in local/SSH metrics and actual custom tab-bar rendering in hidden Kitty."""

import json
import os
from pathlib import Path
import unittest

import test_cwd_integration as cwd_tests
from test_shared_instance_integration import REPO

CPU_ICON, MEMORY_ICON = "\U000f0ee0", "\uefc5"
WORKSPACE_ICON, HOST_ICON = "\uebeb", "\U000f08b9"


@unittest.skipUnless(os.environ.get("KZW_RUN_INTEGRATION") == "1", "hidden Kitty metrics are opt-in")
class MetricsIntegrationTests(unittest.TestCase):
    def check_metrics(self, host):
        renderer = Path(os.environ.get("KZW_TEST_TAB_BAR", Path.home() / ".config/kitty/tab_bar.py"))
        if not renderer.is_file():
            self.skipTest("set KZW_TEST_TAB_BAR to the compatible custom renderer")
        method = "test_remote_tabs_and_splits_inherit_selected_shell_cwd" if host else "test_local_tabs_and_splits_inherit_selected_shell_cwd"
        fixture = cwd_tests.CwdIntegrationTests(method)
        fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(fixture.doCleanups(), "metrics fixture cleanup failed"))
        kit = fixture.kitty
        unrelated = json.loads(kit.raw("ls"))[0]
        origin = ["--host", host] if host else []
        result = kit.kzw("create", "Test", "--attach", *origin)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = fixture.wait(lambda: fixture.windows())[0]
        result = kit.kzw("_managed-create", "Test", "tab", str(first["id"]), first["user_vars"]["kzw_pane"],
                         "--host", host, "--origin", host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fixture.wait(lambda: len(fixture.windows()) == 2)
        tree = json.loads(kit.raw("ls"))
        window = next(ow for ow in tree if ow["id"] != unrelated["id"])
        before = {w["id"]: w["pid"] for w in fixture.windows()}
        shortcuts = str(REPO / "tests/kitty_shortcuts_probe.py")
        keys_before = json.loads(kit.raw("kitten", shortcuts))
        probe = str(REPO / "tests/kitty_metrics_probe.py")

        def inspect(*args):
            return json.loads(kit.raw("kitten", probe, str(window["id"]), *args))

        inspect("install", str(renderer))
        def ready():
            result = inspect()
            record = result["record"]
            return result if record and record["cpu"] is not None and CPU_ICON in result["line"] and MEMORY_ICON in result["line"] else None

        displayed = fixture.wait(ready)
        self.assertTrue(0 <= displayed["record"]["cpu"] <= 100)
        self.assertTrue(0 <= displayed["record"]["ram"] <= 100)
        self.assertEqual(displayed["record"]["origin"], host)
        self.assertEqual(displayed["record"]["workspace"], "Test")
        status = displayed["line"][displayed["line"].index(WORKSPACE_ICON):].rstrip()
        self.assertTrue(status.startswith(WORKSPACE_ICON + " Test · " + HOST_ICON + " "), status)
        self.assertEqual(status.count(" · "), 3)
        self.assertNotIn("  ", status)
        for offset, character in enumerate(status):
            if character == "·":
                self.assertEqual(displayed["fg"][displayed["line"].index(WORKSPACE_ICON) + offset], 0x9399b2)
        for icon, expected in ((WORKSPACE_ICON, 0xcba6f7), (HOST_ICON, 0x89b4fa)):
            self.assertEqual(displayed["fg"][displayed["line"].index(icon)], expected)
        self.assertTrue(all(value is None for value in displayed["other_records"].values()))
        self.assertTrue(displayed["line"].endswith("% "), displayed)
        self.assertNotIn(CPU_ICON, inspect("narrow")["line"])

        receiver = str(REPO / "kzw/metrics_kitten.py")
        for cpu, ram, cpu_color, ram_color in ((0, 100, 0xa6e3a1, 0xf38ba8), (50, 0, 0xf9e2af, 0xa6e3a1)):
            sample = {"workspace": "Test", "origin": host,
                      "metrics": {"host": "Test", "cpu_percent": cpu, "ram_percent": ram, "age_seconds": 0}}
            kit.raw("kitten", receiver, str(window["id"]), json.dumps(sample))
            colored = inspect()
            for icon, expected in ((CPU_ICON, cpu_color), (MEMORY_ICON, ram_color)):
                position = colored["line"].index(icon)
                self.assertEqual(colored["fg"][position], expected)
                self.assertEqual(colored["fg"][position + 2], expected)

        # Positive expiry must redraw an otherwise quiet tab bar too.
        sample = {"workspace": "Test", "origin": host,
                  "metrics": {"host": "Test", "cpu_percent": 42, "ram_percent": 61, "age_seconds": 14.9}}
        kit.raw("kitten", receiver, str(window["id"]), json.dumps(sample))
        def is_expired():
            value = inspect()
            return value if value["record"]["cpu"] is None and CPU_ICON + " —" in value["line"] else None

        expired = fixture.wait(is_expired)
        self.assertIn(MEMORY_ICON + " —", expired["line"])
        self.assertEqual(expired["fg"][expired["line"].index(CPU_ICON)],
                         expired["fg"][expired["line"].index(MEMORY_ICON)])
        self.assertEqual({w["id"]: w["pid"] for w in fixture.windows()}, before)
        remaining = next(ow for ow in json.loads(kit.raw("ls")) if ow["id"] == unrelated["id"])
        self.assertEqual(remaining["tabs"][0]["windows"][0]["pid"], unrelated["tabs"][0]["windows"][0]["pid"])
        keys_after = json.loads(kit.raw("kitten", shortcuts))
        self.assertEqual(keys_after["global"], keys_before["global"])
        self.assertEqual(keys_after["modes"], keys_before["modes"])
        print("Host metrics:", host or "local", displayed["line"].strip())

    def test_local_metrics_rendering_and_expiry(self):
        self.check_metrics("")

    @unittest.skipUnless(os.environ.get("KZW_RUN_SERVER_INTEGRATION") == "1", "SSH metrics are opt-in")
    def test_remote_metrics_rendering_and_expiry(self):
        self.check_metrics(os.environ["KZW_INTEGRATION_HOST"])
