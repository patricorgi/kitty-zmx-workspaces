import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from kzw.viewer_files import refresh


class LivePDFTests(unittest.TestCase):
    def test_rebuild_by_rename_partial_and_hostname_mismatch(self):
        run = subprocess.run

        def ssh(args, **kwargs):
            self.assertEqual(args[-2], "origin")
            return run(shlex.split(args[-1]), **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch("kzw.viewer_files.subprocess.run", side_effect=ssh):
            src = Path(directory) / "it's a file #1.pdf"
            dest = Path(directory) / "preview.pdf"
            cancel = Event()
            args = ("origin", socket.gethostname(), str(src), dest)
            src.write_bytes(b"%PDF-1.5\nfirst\n%%EOF\n")
            stamp = refresh(*args, "", cancel)
            ino = dest.stat().st_ino
            self.assertEqual(refresh(*args, stamp, cancel), stamp)
            self.assertEqual(dest.stat().st_ino, ino)
            src.write_bytes(b"%PDF-1.5\nunfinished")
            with self.assertRaises(OSError):
                refresh(*args, stamp, cancel)
            self.assertEqual(dest.read_bytes(), b"%PDF-1.5\nfirst\n%%EOF\n")
            new = src.with_suffix(".new")
            new.write_bytes(b"%PDF-1.5\nsecond\n%%EOF")
            os.replace(new, src)
            with self.assertRaisesRegex(ValueError, "hostname"):
                refresh("origin", "other.example", str(src), dest, stamp, cancel)
            self.assertEqual(dest.stat().st_ino, ino)
            self.assertNotEqual(refresh(*args, stamp, cancel), stamp)
            self.assertEqual(dest.read_bytes(), src.read_bytes())
