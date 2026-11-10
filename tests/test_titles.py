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
    def test_foreground_process_parsing(self):
        one = f"name={A} pid=10 clients=1\n"
        two = f"name={A}\tpid=10\tclients=1\nname={B}\tpid=20\tclients=1\n"
        cases = (
            ("foreground groups", two, "10 10 12 pts/1 bash\n12 12 12 pts/1 /usr/bin/nvim\n20 20 20 pts/2 /bin/bash\n", {A: "nvim", B: "bash"}),
            ("wrong tty", one, "10 10 12 pts/1 bash\n12 12 12 pts/2 nvim\n", {}),
            ("stale tty", one, "10 10 -1 ? bash\n", {}),
            ("login shell", one, "10 10 10 ttys004 -bash\n", {A: "bash"}),
        )
        for name, sessions, ps, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(parse_processes(sessions, ps), expected)

    def test_active_split_per_tab(self):
        self.assertEqual(active_panes(tree(active=2)), {7: ("host", B, "old")})

    def test_updates_active_title_and_not_stale_focus(self):
        calls = []
        def query(_socket, *args):
            calls.append(args)
            return json.dumps(tree()) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", return_value={A: "nvim", B: "bash"}):
            refresh(Path("socket"), query)
        self.assertIn(("set-tab-title", "--match", "id:7", "nvim │ bash"), calls)
        calls.clear()
        states = iter([tree(active=1), tree(active=2)])
        def changing(_socket, *args):
            calls.append(args)
            return json.dumps(next(states)) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", return_value={A: "nvim", B: "bash"}):
            refresh(Path("socket"), changing)
        self.assertEqual(calls, [("ls",), ("ls",)])

    def test_changed_split_layout_does_not_use_stale_data(self):
        before, after = tree(), tree()
        after[0]["tabs"][0]["windows"].reverse()
        states, calls = iter([before, after]), []
        def query(_socket, *args):
            calls.append(args)
            return json.dumps(next(states)) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", return_value={A: "nvim", B: "bash"}):
            refresh(Path("socket"), query)
        self.assertEqual(calls, [("ls",), ("ls",)])

    def test_unknown_split_and_distinct_hosts(self):
        data = tree()
        data[0]["tabs"][0]["windows"][1]["user_vars"]["kzw_host"] = "other"
        calls = []
        def query(_socket, *args):
            calls.append(args)
            return json.dumps(data) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", side_effect=lambda host: {A: "bash"} if host == "host" else {}):
            refresh(Path("socket"), query)
        self.assertIn(("set-tab-title", "--match", "id:7", "bash │ ?"), calls)

    def test_failed_host_does_not_overwrite_title(self):
        calls = []
        def query(_socket, *args):
            calls.append(args)
            return json.dumps(tree()) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", side_effect=OSError("offline")):
            refresh(Path("socket"), query)
        self.assertEqual(calls, [("ls",), ("ls",)])

    def test_reports_kitty_command_status_when_available(self):
        data = tree()
        data[0]["tabs"][0]["windows"][0]["user_vars"]["kzw_command_state"] = "exit:7"
        calls = []
        def query(_socket, *args):
            calls.append(args)
            return json.dumps(data) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", return_value={A: "bash"}):
            refresh(Path("socket"), query)
        self.assertIn(("set-tab-title", "--match", "id:7", "bash · exit 7 │ ?"), calls)


if __name__ == "__main__":
    unittest.main()
