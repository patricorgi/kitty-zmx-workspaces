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
            env["KITTY_CONFIG_DIRECTORY"] = str(Path(home) / "kitty")
            subprocess.run([sys.executable, str(ROOT / "install.py")], env=env, cwd=home,
                           capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual((Path(home) / "kitty/launch_wrapper.py").resolve(), ROOT / "kzw/native_tab.py")
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

    def test_existing_kitty_hook_is_not_replaced(self):
        from install import install_kitty_hook

        with tempfile.TemporaryDirectory() as config:
            hook = Path(config) / "launch_wrapper.py"
            hook.write_text("# user wrapper")
            with self.assertRaises(FileExistsError):
                install_kitty_hook(config, ROOT)
            self.assertEqual(hook.read_text(), "# user wrapper")

    def test_hook_only_is_idempotent_and_does_not_install_launchers(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / "kitty"
            env = {**os.environ, "HOME": home, "KITTY_CONFIG_DIRECTORY": str(config)}
            for _ in range(2):
                subprocess.run([sys.executable, str(ROOT / "install.py"), "--kitty-hook-only"], env=env,
                               capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual((config / "launch_wrapper.py").resolve(), ROOT / "kzw/native_tab.py")
            self.assertFalse((Path(home) / ".local/bin/kzw").exists())
