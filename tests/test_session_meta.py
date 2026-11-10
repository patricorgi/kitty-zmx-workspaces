import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kzw import session_meta

PANE = "kzw-" + "a" * 32


class SessionMetaTests(unittest.TestCase):
    def test_metadata_is_private_sanitized_and_pid_scoped(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
            session_meta.record(PANE, 123, "git status\x1b[31m\nnext")
            path = Path(directory) / "kzw/session-meta" / (PANE + ".json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(session_meta.read(PANE, 123), "git status [31m next")
            self.assertIsNone(session_meta.read(PANE, 124))
            path.chmod(0o644)
            self.assertIsNone(session_meta.read(PANE, 123))

    def test_invalid_identity_and_unsafe_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
            with self.assertRaises(ValueError):
                session_meta.record("../other", 123, "echo hi")
            root = Path(directory) / "kzw/session-meta"
            root.mkdir(parents=True)
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                session_meta.record(PANE, 123, "echo hi")
            self.assertIsNone(session_meta.read(PANE, 123))
