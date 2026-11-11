import json
import os
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kzw.titles import active_panes, parse_processes, read_process_details, refresh


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
            ("missing child", one, "12 12 12 pts/1 nvim\n", {}),
            ("missing leader", one, "10 10 12 pts/1 bash\n", {}),
            ("wrong group", one, "10 10 12 pts/1 bash\n12 13 12 pts/1 nvim\n", {}),
            ("malformed process rows", one, "bad row\nx y z pts/1 bash\n", {}),
            ("sanitized name", one, "10 10 10 pts/1 a\x1bb" + "x" * 120 + "\n", {A: "ab" + "x" * 98}),
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
        self.assertIn(("set-tab-title", "--match", "id:7", "nvim · bash"), calls)
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
        self.assertIn(("set-tab-title", "--match", "id:7", "bash · ?"), calls)

    def test_failed_host_does_not_overwrite_title(self):
        calls = []
        def query(_socket, *args):
            calls.append(args)
            return json.dumps(tree()) if args == ("ls",) else ""
        with patch("kzw.titles.read_host", side_effect=OSError("offline")):
            refresh(Path("socket"), query)
        self.assertEqual(calls, [("ls",), ("ls",)])

    def test_reports_kitty_command_status_when_available(self):
        cases = (("running", " · running"), ("exit:0", " · exit 0"), ("exit:-9", " · exit -9"),
                 ("exit:7", " · exit 7"), ("exit:1\n", ""), ("exit:" + "1" * 11, ""),
                 ("unknown", ""), (True, ""), (None, ""))
        for state, suffix in cases:
            with self.subTest(state=state):
                data = tree()
                windows = data[0]["tabs"][0]["windows"]
                windows[0]["user_vars"]["kzw_command_state"] = state
                windows[1]["user_vars"]["kzw_command_state"] = "running"
                set_title = Mock()
                with patch("kzw.titles.read_host", return_value={A: "bash", B: "nvim"}) as read_host:
                    refresh(lambda: json.dumps(data), set_title)
                set_title.assert_called_once_with(7, "bash" + suffix + " · nvim")
                read_host.assert_called_once_with("host")

    def test_unchanged_or_cancelled_refresh_never_writes_a_title(self):
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                stop = threading.Event()
                if stopped:
                    stop.set()
                listing = Mock(return_value=json.dumps(tree(title="bash · ?")))
                set_title = Mock()
                with patch("kzw.titles.read_host", return_value={A: "bash"}):
                    refresh(listing, set_title, stop)
                set_title.assert_not_called()
                self.assertEqual(listing.call_count, 1 if stopped else 2)

    def test_process_query_sanitizes_nested_context_and_rejects_untrusted_or_incomplete_input(self):
        output = f"name={A} pid=10 clients=1\n\n__KZW_PROCESS_LIST__\n10 10 10 pts/1 bash\n"
        for host in ("", "user@origin"):
            with self.subTest(host=host), patch.dict(os.environ, {"ZMX_SESSION": A, "ZMX_SESSION_PREFIX": "nested"}), \
                 patch("kzw.titles.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output)) as run:
                self.assertEqual(read_process_details(host), {A: {"name": "bash", "pid": 10}})
                command = run.call_args.args[0]
                self.assertEqual(command[0], "ssh" if host else "sh")
                if host:
                    self.assertEqual(command[1:6], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host])
                self.assertIn("unset ZMX_SESSION ZMX_SESSION_PREFIX", command[-1])
                self.assertNotIn("ZMX_SESSION", run.call_args.kwargs["env"])
                self.assertNotIn("ZMX_SESSION_PREFIX", run.call_args.kwargs["env"])
        with patch("kzw.titles.subprocess.run") as run:
            for host in ("-bad", "origin;command", "host\nother"):
                with self.subTest(host=host), self.assertRaises(ValueError):
                    read_process_details(host)
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess([], 0, "incomplete")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                read_process_details("")


if __name__ == "__main__":
    unittest.main()
