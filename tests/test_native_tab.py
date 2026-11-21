import unittest

from kzw.native_tab import check_target


class NativeTabTests(unittest.TestCase):
    def test_only_accept_a_new_tab_in_the_exact_source_workspace_window(self):
        env = {"KITTY_LISTEN_ON": "unix:/kitty", "KZW_WORKSPACE": "Test",
               "KZW_WORKSPACE_DIRECTORY": "/state/Test", "KZW_HOST": "host"}
        source = {"id": 10, "user_vars": {"kzw_pane": "pane", "kzw_workspace": "Test",
                                          "kzw_directory": "/state/Test", "kzw_host": "host"}}
        target = {"id": 11}
        a, b = {"id": 1, "windows": [source]}, {"id": 2, "windows": [target]}
        view = {"instance_pid": 123, "address": "unix:/kitty", "os_window_id": 1}

        def check(tree, **overrides):
            return check_target({**view, **overrides}, tree, 123, 10, "pane", 11, env)

        tree = [{"id": 1, "tabs": [a, b]}]
        self.assertTrue(check(tree))
        self.assertFalse(check(tree, instance_pid=124))
        self.assertFalse(check(tree, address="unix:/other"))
        self.assertFalse(check([{"id": 1, "tabs": [a]}, {"id": 2, "tabs": [b]}]))
        self.assertFalse(check([{"id": 1, "tabs": [{"id": 1, "windows": [source, target]}]}]))
        self.assertFalse(check([{"id": 1, "tabs": [a, {"id": 2, "windows": [target, {"id": 12}]}]}]))
        source["user_vars"]["kzw_origin"] = "other"
        self.assertFalse(check(tree))
