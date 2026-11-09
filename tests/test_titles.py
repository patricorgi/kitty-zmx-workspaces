import json
import unittest
from pathlib import Path
from unittest.mock import patch

from kzw.titles import active_panes, parse_processes, refresh


A = "kzw-" + "a" * 32
B = "kzw-" + "b" * 32


def tree(active=1, title="old"):
    return [{"tabs": [{"id": 7, "title": title, "windows": [
        {"id": 1, "is_active": active == 1, "user_vars": {"kzw_host": "host", "kzw_pane": A}},
        {"id": 2, "is_active": active == 2, "user_vars": {"kzw_host": "host", "kzw_pane": B}},
    ]}]}]


class TitleTests(unittest.TestCase):
    def test_foreground_process_from_pty_group(self):
        sessions = f"name={A}\tpid=10\tclients=1\nname={B}\tpid=20\tclients=1\n"
        ps = "10 10 12 pts/1 bash\n12 12 12 pts/1 /usr/bin/nvim\n20 20 20 pts/2 /bin/bash\n"
        self.assertEqual(parse_processes(sessions, ps), {A: "nvim", B: "bash"})

    def test_reject_stale_or_wrong_tty(self):
        sessions = f"name={A} pid=10 clients=1\n"
        self.assertEqual(parse_processes(sessions, "10 10 12 pts/1 bash\n12 12 12 pts/2 nvim\n"), {})
        self.assertEqual(parse_processes(sessions, "10 10 -1 ? bash\n"), {})

    def test_active_split_per_tab(self):
        self.assertEqual(active_panes(tree(active=2)), {7: ("host", B, "old")})

    def test_updates_active_title_and_not_stale_focus(self):
        calls = []
        def query(_socket, *args):
            calls.append(args)
            return json.dumps(tree()) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", return_value={A: "nvim", B: "bash"}):
            refresh(Path("socket"), query)
        self.assertIn(("set-tab-title", "--match", "id:7", "nvim"), calls)
        calls.clear()
        states = iter([tree(active=1), tree(active=2)])
        def changing(_socket, *args):
            calls.append(args)
            return json.dumps(next(states)) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", return_value={A: "nvim", B: "bash"}):
            refresh(Path("socket"), changing)
        self.assertEqual(calls, [("ls",), ("ls",)])

    def test_failed_host_does_not_overwrite_title(self):
        calls = []
        def query(_socket, *args):
            calls.append(args)
            return json.dumps(tree()) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", side_effect=OSError("offline")):
            refresh(Path("socket"), query)
        self.assertEqual(calls, [("ls",), ("ls",)])


if __name__ == "__main__":
    unittest.main()
