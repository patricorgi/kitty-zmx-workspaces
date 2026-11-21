from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kzw import cli


class AttachPreviewTests(unittest.TestCase):
    def test_preview_precedes_ssh_and_canceled_preview_releases_lease(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(cli, "state_dir", return_value=Path(root)), \
                patch.object(cli.shutil, "which", return_value="/bin/kzw-pane"), \
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
