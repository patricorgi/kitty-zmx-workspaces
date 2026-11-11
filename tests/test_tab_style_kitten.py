import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import kzw.titles as titles

PANE = "kzw-" + "a" * 32


def load_kitten():
    holder = {}
    handler = types.ModuleType("kittens.tui.handler")
    handler.result_handler = lambda **kwargs: lambda function: function
    boss_module = types.ModuleType("kitty.boss")
    boss_module.get_options = lambda: holder["options"]
    with patch.dict(sys.modules, {"kittens": types.ModuleType("kittens"),
                                 "kittens.tui": types.ModuleType("kittens.tui"),
                                 "kittens.tui.handler": handler,
                                 "kitty": types.ModuleType("kitty"),
                                 "kitty.boss": boss_module}):
        spec = importlib.util.spec_from_file_location(
            "test_tab_style_kitten_impl", Path(__file__).parents[1] / "kzw/tab_style_kitten.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, holder


def options():
    return types.SimpleNamespace(
        tab_title_template="user {title}",
        active_tab_title_template=None,
        active_tab_font_style=(True, True),
        inactive_tab_font_style=(False, False))


def boss(panes=()):
    windows = [types.SimpleNamespace(user_vars={"kzw_pane": pane}) for pane in panes]
    manager = types.SimpleNamespace(tabs=[windows], refresh_active_tab_bar=Mock())
    fake = types.SimpleNamespace(os_window_map={1: manager}, apply_new_options=Mock())
    return fake


class TabStyleKittenTests(unittest.TestCase):
    def call(self, kitten, action, path, fake=None):
        return kitten.handle_result(["kitten", action, str(path)], None, 1, fake or boss())

    def test_template_matches_private_instance(self):
        kitten, _ = load_kitten()
        self.assertEqual(kitten.TEMPLATE, titles.TAB_TITLE_TEMPLATE)

    def test_apply_saves_originals_once_and_sets_plain_index(self):
        kitten, holder = load_kitten()
        holder["options"] = opts = options()
        fake = boss()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "style.json"
            self.assertEqual(self.call(kitten, "apply", path, fake), "applied")
            saved = json.loads(path.read_text())
            self.assertEqual(saved["tab_title_template"], "user {title}")
            self.assertIsNone(saved["active_tab_title_template"])
            self.assertEqual(opts.tab_title_template, kitten.TEMPLATE)
            self.assertEqual(opts.active_tab_title_template, kitten.TEMPLATE)
            self.assertEqual(opts.active_tab_font_style, (True, True))
            fake.apply_new_options.assert_called_once_with(opts)

            # A second attach must not overwrite the user's originals.
            self.call(kitten, "apply", path, fake)
            self.assertEqual(json.loads(path.read_text()), saved)

    def test_restore_waits_for_other_managed_views(self):
        kitten, holder = load_kitten()
        holder["options"] = opts = options()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "style.json"
            self.call(kitten, "apply", path, boss())
            self.assertEqual(self.call(kitten, "restore", path, boss([PANE])), "busy")
            self.assertTrue(path.exists())
            self.assertEqual(opts.tab_title_template, kitten.TEMPLATE)

    def test_restore_recovers_the_exact_user_settings(self):
        kitten, holder = load_kitten()
        holder["options"] = opts = options()
        fake = boss()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "style.json"
            self.call(kitten, "apply", path, fake)
            self.assertEqual(self.call(kitten, "restore", path, fake), "restored")
            self.assertFalse(path.exists())
            self.assertEqual(opts.tab_title_template, "user {title}")
            self.assertIsNone(opts.active_tab_title_template)
            self.assertEqual(opts.active_tab_font_style, (True, True))
            self.assertEqual(opts.inactive_tab_font_style, (False, False))

            # Nothing left to restore.
            self.assertEqual(self.call(kitten, "restore", path, fake), "unchanged")

    def test_invalid_requests_fail_closed(self):
        kitten, holder = load_kitten()
        holder["options"] = options()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "style.json"
            with self.assertRaises(ValueError):
                kitten.handle_result(["kitten", "apply"], None, 1, boss())
            path.symlink_to(Path(tmp) / "other")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                self.call(kitten, "apply", path)
            path.unlink()
            path.write_text('{"tab_title_template": 1}')
            with self.assertRaisesRegex(ValueError, "invalid tab-style"):
                self.call(kitten, "restore", path)

    def test_restore_preserves_user_changes_made_while_attached(self):
        for changed in (("tab_title_template",), ("active_tab_title_template",), ("tab_title_template", "active_tab_title_template")):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                kitten, holder = load_kitten()
                holder["options"] = opts = options()
                path = Path(tmp) / "style.json"
                fake = boss()
                self.call(kitten, "apply", path, fake)
                for key in changed:
                    setattr(opts, key, "user changed " + key)
                self.call(kitten, "restore", path, fake)
                for key in ("tab_title_template", "active_tab_title_template"):
                    expected = "user changed " + key if key in changed else getattr(options(), key)
                    self.assertEqual(getattr(opts, key), expected)
                self.assertFalse(path.exists())

    def test_failed_originals_write_keeps_user_settings_and_removes_temporary_file(self):
        kitten, holder = load_kitten()
        holder["options"] = opts = options()
        fake = boss()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "style.json"
            with patch.object(kitten.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                self.call(kitten, "apply", path, fake)
            self.assertEqual(opts.tab_title_template, "user {title}")
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).glob(".tab-style-*")), [])
            fake.apply_new_options.assert_not_called()

    def test_older_kitty_options_and_refresh_fallback_restore_exactly(self):
        kitten, _ = load_kitten()
        kitten.get_options = None
        manager = types.SimpleNamespace(tabs=[], refresh_active_tab_bar=Mock())
        fake = types.SimpleNamespace(opts=options(), os_window_map={1: manager})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "style.json"
            self.call(kitten, "apply", path, fake)
            self.assertEqual(fake.opts.tab_title_template, kitten.TEMPLATE)
            self.call(kitten, "restore", path, fake)
        self.assertEqual(fake.opts.tab_title_template, "user {title}")
        self.assertEqual(manager.refresh_active_tab_bar.call_count, 2)


if __name__ == "__main__":
    unittest.main()
