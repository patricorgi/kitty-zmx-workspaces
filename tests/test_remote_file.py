import unittest
import json
from types import SimpleNamespace
from unittest.mock import Mock

from test_kitten_routing import A, kitten


class Window:
    handle_remote_file = lambda self, host, path: None
    destroyed = False


class RemoteFileTests(unittest.TestCase):
    def setUp(self):
        self.module = kitten("remote_file")
        self.w = Window()
        self.w.user_vars = {"kzw_host": "login", "kzw_workspace": "Test", "kzw_pane": A}
        self.w.child = SimpleNamespace(cmdline=["/path/Python", "-m", "kzw.pane", "--pane", A, "--host", "login",
                                                "--existing"], environ={"KZW_HOST": "login", "KZW_WORKSPACE": "Test"})
        self.boss = Mock()

    def test_remote_file_menu_pins_host_instead_of_trusting_link_or_foreground(self):
        self.assertTrue(self.module.install(self.boss, self.w))
        self.assertFalse(self.module.install(self.boss, self.w))
        self.w.user_vars["kzw_host"] = "different"
        self.w.handle_remote_file("nested.example", "/file with spaces")
        self.boss.run_kitten_with_metadata.assert_called_once_with("remote_file", [
            "--hostname", "nested.example", "--path", "/file with spaces",
            "--ssh-connection-data", '["ssh", "login", 0, ""]',
        ], window=self.w)

    def test_ignore_unmanaged_or_mismatched_children_even_with_remote_vars(self):
        for cmd in (["bash"], ["ssh", "login"], ["/path/kzw-pane", "--pane", A, "--host", "different"]):
            with self.subTest(cmd=cmd):
                self.w.child.cmdline = cmd
                self.assertFalse(self.module.install(self.boss, self.w))
        self.w.child.cmdline = ["/path/kzw-pane"]
        self.w.user_vars["kzw_host"] = "-option"
        self.assertFalse(self.module.install(self.boss, self.w))
        self.w.user_vars["kzw_host"] = "login"
        self.assertTrue(self.module.install(self.boss, self.w))
        self.w.handle_remote_file("login", "/bad\npath")
        self.boss.run_kitten_with_metadata.assert_not_called()

    def test_menu_request_is_ephemeral_and_restricted_to_active_pinned_window(self):
        menu = kitten("remote_file_menu")
        self.w.watchers = SimpleNamespace(copy=lambda: SimpleNamespace(on_set_user_var=[]))
        self.boss.active_window = self.w
        self.assertEqual(menu.install(self.boss, self.w), "login")
        menu.install(self.boss, self.w)
        watcher, = self.w.watchers.on_set_user_var
        def request(value):
            self.w.user_vars[menu.REQUEST] = value
            watcher(self.boss, self.w, {"key": menu.REQUEST, "value": value})
            self.assertNotIn(menu.REQUEST, self.w.user_vars)
        for malformed in ("{", "{}", '["", "/file"]', '["login", "relative"]', '["host", "/a\\nb"]'):
            request(malformed)
        good = json.dumps(["nested.example", "/file #1"])
        self.boss.active_window = None
        request(good)
        self.boss.run_kitten_with_metadata.assert_not_called()
        self.boss.active_window = self.w
        request(good)
        self.boss.run_kitten_with_metadata.assert_called_once_with("remote_file", [
            "--hostname", "nested.example", "--path", "/file #1",
            "--ssh-connection-data", '["ssh", "login", 0, ""]',
        ], window=self.w)
