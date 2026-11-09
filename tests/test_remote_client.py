import json
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from kzw import remote_client
from kzw.remote import Store

PANE = "kzw-" + "a" * 32
SNAPSHOT = ("# kzw-focus {\"pane\":\"" + PANE + "\"}\n"
            "new_tab Test\nlayout splits\ncd /old/computer\n"
            "launch 'kitty-unserialize-data={\"id\":1}' --cwd=/old/computer "
            "--var=kzw_host=origin /old/bin/kzw-pane --pane " + PANE + " --host origin --existing\n")


class RemoteClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "server" / "workspace.sqlite")
        self.patch = patch("kzw.remote_client.call", side_effect=lambda host, req: self.store.dispatch(req))
        self.patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.patch.stop)

    def directory(self, name):
        path = self.root / name
        path.mkdir()
        return path

    def test_create_publish_pull_from_fresh_client(self):
        first = self.directory("first")
        second = self.directory("second")
        remote_client.create(first, "Test", "origin")
        self.assertEqual(self.store.dispatch({"method": "workspace.list"})["workspaces"][0]["name"], "Test")
        remote_client.publish(first, SNAPSHOT, [PANE])
        saved = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertNotIn("/old/computer", saved["snapshot"])
        self.assertNotIn("/old/bin/kzw-pane", saved["snapshot"])
        remote_client.pull(second, "Test", "origin", "/new/bin/kzw-pane")
        restored = (second / "workspace.kitty-session").read_text()
        self.assertIn("/new/bin/kzw-pane", restored)
        self.assertIn(PANE, restored)
        self.assertEqual(json.loads((second / "panes.json").read_text()), [PANE])

    def test_lost_ack_replayed_without_duplicate_revision(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        def lost_ack(host, req):
            self.store.dispatch(req)
            raise OSError("reply lost")
        with patch("kzw.remote_client.call", side_effect=lost_ack):
            with self.assertRaises(OSError):
                remote_client.publish(directory, SNAPSHOT, [PANE])
        self.assertIn("pending", json.loads((directory / "remote.json").read_text()))
        remote_client.publish(directory, SNAPSHOT, [PANE])
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["revision"], 2)

    def test_create_retry_after_lost_ack(self):
        directory = self.directory("first")
        def call_with_lost_create(host, req):
            result = self.store.dispatch(req)
            if req["method"] == "workspace.put":
                raise OSError("reply lost")
            return result
        with patch("kzw.remote_client.call", side_effect=call_with_lost_create):
            with self.assertRaises(OSError):
                remote_client.create(directory, "Test", "origin")
        remote_client.create(directory, "Test", "origin")
        remote = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertEqual(remote["revision"], 1)
        self.assertEqual(remote["panes"], [])

    def test_stale_revision_conflict_does_not_replace_remote(self):
        first = self.directory("first")
        second = self.directory("second")
        remote_client.create(first, "Test", "origin")
        remote_client.pull(second, "Test", "origin", "/bin/kzw-pane")
        remote_client.publish(first, SNAPSHOT, [PANE])
        with self.assertRaises(ValueError):
            remote_client.publish(second, SNAPSHOT.replace("new_tab Test", "new_tab stale"), [PANE])
        remote = self.store.dispatch({"method": "workspace.get", "workspace": "Test"})
        self.assertNotIn("stale", remote["snapshot"])

    def test_reject_mixed_host_and_foreign_launch(self):
        with self.assertRaises(ValueError):
            remote_client.translate(SNAPSHOT, "", "different", True)
        with self.assertRaises(ValueError):
            remote_client.translate("launch echo dangerous\n", "/bin/kzw-pane", "origin", False)

    def test_existing_local_workspace_is_never_overwritten(self):
        directory = self.directory("first")
        (directory / "workspace.kitty-session").write_text("existing")
        with self.assertRaises(ValueError):
            remote_client.create(directory, "Test", "origin")
        self.assertEqual((directory / "workspace.kitty-session").read_text(), "existing")

    def test_prune_requires_confirmed_absence_on_origin(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.publish(directory, SNAPSHOT, [PANE])
        live = subprocess.CompletedProcess([], 0, stdout=PANE + "\n", stderr="")
        with patch("kzw.remote.subprocess.run", return_value=live):
            with self.assertRaises(ValueError):
                remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_exits=True)
        self.assertTrue(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"])
        absent = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("kzw.remote.subprocess.run", return_value=absent):
            remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_exits=True)
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [])

    def test_prune_listing_failure_preserves_remote_manifest(self):
        directory = self.directory("first")
        remote_client.create(directory, "Test", "origin")
        remote_client.publish(directory, SNAPSHOT, [PANE])
        with patch("kzw.remote.subprocess.run", side_effect=OSError("zmx unavailable")):
            with self.assertRaises(OSError):
                remote_client.publish(directory, "# kzw-empty-workspace\n", [], confirmed_exits=True)
        self.assertEqual(self.store.dispatch({"method": "workspace.get", "workspace": "Test"})["panes"], [PANE])
