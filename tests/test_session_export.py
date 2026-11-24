"""Lifecycle callbacks must not make verified managed terminals disappear."""

from types import SimpleNamespace
import unittest

from kzw.session_export import ExportWindow, HOOKS, serializable_managed_windows


PANE = "kzw-" + "a" * 32


class FakeWindow:
    def __init__(self, window_id=7):
        self.id = window_id
        self.user_vars = {"kzw_pane": PANE, "kzw_host": "origin"}
        self.creation_spec = SimpleNamespace(
            cmd=["/bin/kzw", "pane", "--pane", PANE, "--host", "origin", "--existing"], overlay_for=None)
        self.window_custom_type = ""
        self.actions_on_close = [lambda *args: None]
        self.actions_on_focus_change = [lambda *args: None]
        self.actions_on_removal = [lambda *args: None]

    def as_launch_command(self, options, cwd, **kwargs):
        if any(getattr(self, hook) for hook in HOOKS):
            return []
        return ["launch", str(self.id), cwd, self.user_vars["kzw_pane"]]


class SessionExportTests(unittest.TestCase):
    def export(self, windows):
        return serializable_managed_windows(windows, FakeWindow.as_launch_command)

    def test_callbacks_are_present_and_unchanged_during_and_after_export(self):
        window = FakeWindow()
        hooks = {name: getattr(window, name) for name in HOOKS}
        self.assertEqual(window.as_launch_command(None, ""), [])
        with self.export([window]):
            self.assertEqual(window.as_launch_command(None, "/work"), ["launch", "7", "/work", PANE])
            for name, callbacks in hooks.items():
                self.assertIs(getattr(window, name), callbacks)
                self.assertEqual(len(callbacks), 1)
        self.assertNotIn("as_launch_command", vars(window))
        self.assertEqual(window.as_launch_command(None, ""), [])

    def test_each_lifecycle_hook_is_supported_independently(self):
        for hook in HOOKS:
            window = FakeWindow()
            for name in HOOKS:
                setattr(window, name, [lambda: None] if name == hook else [])
            with self.export([window]):
                self.assertTrue(window.as_launch_command(None, ""))

    def test_error_restores_exact_bound_method_behavior(self):
        window = FakeWindow()
        original = window.as_launch_command
        with self.assertRaisesRegex(RuntimeError, "serializer failed"):
            with self.export([window]):
                raise RuntimeError("serializer failed")
        self.assertEqual(window.as_launch_command, original)
        self.assertNotIn("as_launch_command", vars(window))

    def test_later_invalid_pane_restores_earlier_patch(self):
        first, second = FakeWindow(), FakeWindow(8)
        second.creation_spec.overlay_for = first.id
        with self.assertRaisesRegex(ValueError, "not a verified"):
            with self.export([first, second]):
                self.fail("invalid snapshot accepted")
        self.assertNotIn("as_launch_command", vars(first))

    def test_unmanaged_overlay_custom_ui_and_mismatched_command_are_rejected(self):
        for mode in ("unmanaged", "overlay", "custom ui", "wrong pane", "wrong host", "not launcher", "no spec"):
            with self.subTest(mode=mode):
                window = FakeWindow()
                if mode == "unmanaged":
                    window.user_vars = {}
                elif mode == "overlay":
                    window.creation_spec.overlay_for = 1
                elif mode == "custom ui":
                    window.window_custom_type = "kitten"
                elif mode == "wrong pane":
                    window.creation_spec.cmd[2] = "kzw-" + "b" * 32
                elif mode == "wrong host":
                    window.creation_spec.cmd[4] = "another-origin"
                elif mode == "not launcher":
                    window.creation_spec.cmd[0] = "/bin/bash"
                else:
                    window.creation_spec = None
                with self.assertRaisesRegex(ValueError, "not a verified"):
                    with self.export([window]):
                        pass
                self.assertNotIn("as_launch_command", vars(window))

    def test_python_module_launcher_is_supported(self):
        window = FakeWindow()
        window.creation_spec.cmd[:1] = ["/bin/python3", "-m", "kzw.pane"]
        with self.export([window]):
            self.assertTrue(window.as_launch_command(None, ""))

    def test_custom_serializer_is_not_bypassed_or_replaced(self):
        window = FakeWindow()
        original = window.as_launch_command = lambda *args, **kwargs: []
        with self.assertRaisesRegex(ValueError, "custom session serializer"):
            with self.export([window]):
                pass
        self.assertIs(window.as_launch_command, original)

    def test_unhooked_windows_are_never_patched(self):
        window = FakeWindow()
        for name in HOOKS:
            setattr(window, name, [])
        with self.export([window]):
            self.assertNotIn("as_launch_command", vars(window))
            self.assertTrue(window.as_launch_command(None, ""))

    def test_proxy_delegates_without_changing_real_window(self):
        window = FakeWindow()
        proxy = ExportWindow(window)
        self.assertEqual(proxy.id, window.id)
        self.assertIs(proxy.creation_spec, window.creation_spec)
        for hook in HOOKS:
            self.assertEqual(getattr(proxy, hook), ())
            self.assertEqual(len(getattr(window, hook)), 1)


if __name__ == "__main__":
    unittest.main()
