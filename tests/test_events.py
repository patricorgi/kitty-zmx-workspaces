"""Read-only remote inspection and resumable journal streaming."""

import io
import json
import unittest
from unittest.mock import patch

from kzw.cli import events_remote, inspect_remote


A = "kzw-" + "a" * 32
SNAPSHOT = {"exists": True, "revision": 2, "snapshot": "portable layout\n", "panes": [A],
            "event_cursor": 8, "reservations": [], "staged": None}


class RemoteEventsTests(unittest.TestCase):
    def test_inspect_exposes_snapshot_and_cursor_without_mutation(self):
        with patch("kzw.cli.remote_client.call", return_value=SNAPSHOT) as call, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            inspect_remote("Test", "origin", as_json=True)
        self.assertEqual(json.loads(output.getvalue())["event_cursor"], 8)
        self.assertEqual(call.call_args.args[1], {"method": "workspace.sync", "workspace": "Test"})

    def test_events_default_once_yields_atomic_snapshot(self):
        with patch("kzw.cli.remote_client.call", return_value=SNAPSHOT) as call, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            events_remote("Test", "origin", once=True, as_json=True)
        event = json.loads(output.getvalue())
        self.assertEqual((event["type"], event["revision"], event["event_cursor"]), ("snapshot", 2, 8))
        self.assertEqual(call.call_count, 1)

    def test_events_resume_from_cursor_and_emit_ordered_records(self):
        batch = {"events": [{"seq": 7, "revision": 1, "kind": "session.missing", "data": {"pane": A}},
                            {"seq": 8, "revision": 2, "kind": "layout.updated", "data": {"panes": []}}]}
        with patch("kzw.cli.remote_client.call", side_effect=[SNAPSHOT, batch]) as call, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            events_remote("Test", "origin", after=6, once=True, as_json=True)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event["seq"] for event in events], [7, 8])
        self.assertEqual([event["type"] for event in events], ["session.missing", "layout.updated"])
        self.assertEqual(call.call_args.args[1]["after"], 6)

    def test_events_reject_future_or_out_of_order_cursors(self):
        with patch("kzw.cli.remote_client.call", return_value=SNAPSHOT):
            with self.assertRaisesRegex(ValueError, "ahead"):
                events_remote("Test", "origin", after=9, once=True)
        batch = {"events": [{"seq": 6, "revision": 2, "kind": "x", "data": {}}]}
        with patch("kzw.cli.remote_client.call", side_effect=[SNAPSHOT, batch]):
            with self.assertRaisesRegex(ValueError, "out-of-order"):
                events_remote("Test", "origin", after=6, once=True)

    def test_interrupted_stream_retries_from_last_delivered_cursor(self):
        first = {"events": [{"seq": 9, "revision": 3, "kind": "layout.updated", "data": {}}]}
        with patch("kzw.cli.remote_client.call", side_effect=[SNAPSHOT, first, OSError("lost"), KeyboardInterrupt]) as call, \
             patch("kzw.cli.time.sleep"), patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(KeyboardInterrupt):
                events_remote("Test", "origin", as_json=True)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([message["type"] for message in messages], ["snapshot", "layout.updated"])
        self.assertEqual([call.args[1].get("after") for call in call.call_args_list[1:]], [8, 9, 9])

    def test_replay_drains_multiple_pages_without_losing_cursor(self):
        snapshot = {**SNAPSHOT, "event_cursor": 101}
        page = {"events": [{"seq": seq, "revision": 1, "kind": "test", "data": {}}
                           for seq in range(1, 101)]}
        last = {"events": [{"seq": 101, "revision": 2, "kind": "test", "data": {}}]}
        with patch("kzw.cli.remote_client.call", side_effect=[snapshot, page, last]) as call, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            events_remote("Test", "origin", after=0, once=True, as_json=True)
        self.assertEqual(len(output.getvalue().splitlines()), 101)
        self.assertEqual([item.args[1].get("after") for item in call.call_args_list[1:]], [0, 100])
