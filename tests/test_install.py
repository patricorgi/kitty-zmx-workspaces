import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_install_launchers_work_from_another_directory(self):
        with tempfile.TemporaryDirectory(prefix="kzw home ") as home:
            env = os.environ.copy()
            env.update(HOME=home, PATH=str(Path(sys.executable).resolve().parent) + os.pathsep + "/usr/bin:/bin")
            env.pop("PYTHONPATH", None)
            subprocess.run([sys.executable, str(ROOT / "install.py")], env=env, cwd=home,
                           capture_output=True, text=True, timeout=10, check=True)
            for name in ("kzw", "kzw-pane"):
                result = subprocess.run([str(Path(home) / ".local/bin" / name), "--help"], env=env,
                                        cwd=home, capture_output=True, text=True, timeout=10, check=True)
                self.assertIn("usage:", result.stdout)

    def test_install_refuses_existing_file_and_dangling_symlink(self):
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as home:
                bindir = Path(home) / ".local/bin"
                bindir.mkdir(parents=True)
                target = bindir / "kzw-pane"
                if kind == "file":
                    target.write_text("preserve me")
                else:
                    target.symlink_to("missing")
                env = {**os.environ, "HOME": home}
                result = subprocess.run([sys.executable, str(ROOT / "install.py")], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((bindir / "kzw").exists())
                self.assertTrue(os.path.lexists(target))
                if kind == "file":
                    self.assertEqual(target.read_text(), "preserve me")
