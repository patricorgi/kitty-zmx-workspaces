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
            self.assertFalse((Path(home) / "kitty").exists())
            self.assertEqual([p.name for p in (Path(home) / ".local/bin").iterdir()], ["kzw"])
            for args in ([], ["pane"]):
                result = subprocess.run([str(Path(home) / ".local/bin/kzw"), *args, "--help"], env=env,
                                        cwd=home, capture_output=True, text=True, timeout=10, check=True)
                self.assertIn("usage:", result.stdout)

    def test_install_refuses_existing_file_and_dangling_symlink(self):
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as home:
                bindir = Path(home) / ".local/bin"
                bindir.mkdir(parents=True)
                target = bindir / "kzw"
                if kind == "file":
                    target.write_text("preserve me")
                else:
                    target.symlink_to("missing")
                env = {**os.environ, "HOME": home}
                result = subprocess.run([sys.executable, str(ROOT / "install.py")], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
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
            config.mkdir()
            conf = config / "kitty.conf"
            conf.write_text("map f9 new_tab\n")
            env = {**os.environ, "HOME": home, "KITTY_CONFIG_DIRECTORY": str(config)}
            bindir = Path(home) / "custom bin"
            install_cmd = [sys.executable, str(ROOT / "install.py"), "--bin-dir", str(bindir)]
            subprocess.run(install_cmd, env=env, cwd=home, check=True, capture_output=True)
            for _ in range(2):
                subprocess.run([*install_cmd, "--kitty-hook-only"], env=env,
                               capture_output=True, text=True, timeout=10, check=True)
            hook = (config / "launch_wrapper.py").resolve()
            self.assertTrue(hook.is_file() and hook.is_relative_to(Path(home).resolve()))
            self.assertTrue(conf.read_text().startswith("map f9 new_tab\n"))
            watchers = [Path(line[8:]).resolve() for line in conf.read_text().splitlines() if line.startswith("watcher ")]
            self.assertEqual(watchers, [hook.with_name("native_close.py")])
            self.assertFalse((Path(home) / ".local/bin/kzw").exists())
