import os
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from kzw import cli
from kzw.attach_preview import show_preview, splash_frames, splash_message, splash_screen


class AttachPreviewTests(unittest.TestCase):
    def test_custom_splash_is_plain_multiline_text(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            path = Path(root) / "kzw/splash.txt"
            default = "Connecting to Test (origin)..."
            self.assertEqual(splash_message("Test", "origin"), default)
            path.parent.mkdir()
            path.write_text("准备 {workspace}\n\nConnecting to {host}\n{unknown}\x07", encoding="utf-8")
            self.assertEqual(splash_message("Test", "origin"), "准备 Test\n\nConnecting to origin\n{unknown}")
            for content in (b"\xff", b" \n", b"x" * 8193):
                path.write_bytes(content)
                self.assertEqual(splash_message("Test", "origin"), default)

    def test_renderer_centers_block_preserves_blank_lines_and_clips(self):
        self.assertEqual(splash_screen("准备\n\nDone", 20, 10),
                         "\x1b[?25l\x1b[4;9H准备\x1b[5;9H\x1b[6;9HDone")
        self.assertEqual(splash_screen("准备 x\nsecond\nthird", 3, 2),
                          "\x1b[?25l\x1b[1;1H准\x1b[2;1Hsec")

    def test_animation_loops_in_order_with_per_frame_holds(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / "splash.frames").write_text(
                "@frame 50\n⣿ {workspace}\n\none\n@frame 400\n{host}\x07\n", encoding="utf-8")
            with patch("kzw.attach_preview.render_frame") as paint, \
                    patch("sys.stdout"), \
                    patch("time.sleep", side_effect=[None, None, KeyboardInterrupt]) as hold:
                with self.assertRaises(KeyboardInterrupt):
                    show_preview(directory, "Test", "local")
            first = call("⣿ Test\n\none", redraw=True)
            self.assertEqual(paint.call_args_list, [first, call("local", redraw=True), first])
            self.assertEqual(hold.call_args_list, [call(.05), call(.4), call(.05)])

    def test_bad_animation_falls_back_to_static_text(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            path = directory / "splash.frames"
            (directory / "splash.txt").write_text("Loading {workspace}")
            for data in (b"", b"\xff", b"hi\n@frame 50\nok", b"@frame 0\nx", b"@frame 10001\nx",
                         b"@frame 50\nok\n@frame 100\n \n", b"@frame 100\n" + b"x" * 8193,
                         b"@frame 50\nx\n" * 257, b"x" * 262145):
                with self.subTest(data=data[:20]):
                    path.write_bytes(data)
                    self.assertEqual(splash_frames(directory, "Test", "local"), [])
            with patch("kzw.attach_preview.render_frame") as paint, patch("os.execl"):
                show_preview(directory, "Test", "local")
            paint.assert_called_once_with("Loading Test")

    def test_preview_precedes_ssh_and_canceled_preview_releases_lease(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(cli, "state_dir", return_value=Path(root)), \
                patch.object(cli.shutil, "which", return_value="/bin/kzw"), \
                patch("kzw.attach_preview.AttachPreview") as factory, \
                patch.object(cli, "startup_status") as status, \
                patch.object(cli.remote_client, "reuse_configured_ssh_once", return_value=nullcontext()), \
                patch.object(cli.remote_client, "acquire_lease") as lease, \
                patch.object(cli.remote_client, "release_lease") as release, \
                patch.object(cli.remote_client, "pull") as pull, \
                patch.object(cli, "run") as run:
            order = Mock()
            order.attach_mock(factory, "preview")
            order.attach_mock(status, "status")
            order.attach_mock(lease, "lease")
            preview = factory.return_value
            preview.populated = False
            preview.command.side_effect = ValueError("preview closed")
            with self.assertRaisesRegex(ValueError, "preview closed"):
                cli.attach_remote("Test", "origin", show_preview=True)
            self.assertEqual([c[0] for c in order.mock_calls[:3]], ["preview", "status", "lease"])
            release.assert_called_once_with(Path(root))
            pull.assert_not_called()
            run.assert_not_called()
