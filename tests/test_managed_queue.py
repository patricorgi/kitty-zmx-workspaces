"""Queue tabs use the same durable ownership and attachment contract as shells."""

import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kzw import cli, pane, remote_client, sessions, utility
from kzw.core import pane_map, rewrite_export
from kzw.remote import Store, layout_sessions

A, B = ("kzw-" + c * 32 for c in "ab")


def tree(host, queue=False):
    tabs = [{"id": 1, "is_active": not queue, "windows": [{"id": 7, "is_active": True,
             "user_vars": {"kzw_pane": A, "kzw_host": host}}]}]
    if queue:
        tabs.append({"id": 2, "is_active": True, "windows": [{"id": 8, "is_active": True,
                     "user_vars": {"kzw_pane": B, "kzw_host": host}}]})
    return [{"id": 42, "last_focused": True, "tabs": tabs}]


class ManagedQueueTests(unittest.TestCase):
    def test_invalid_queue_program_or_split_is_rejected_before_mutation(self):
        for kind, program in (("split", "condor-queue"), ("tab", "arbitrary-command")):
            with self.subTest(kind=kind, program=program), patch("kzw.cli.state_dir") as state:
                with self.assertRaisesRegex(ValueError, "initial tab program"):
                    cli.create_pane("Test", kind, 7, A, "", initial_program=program)
                state.assert_not_called()

    def test_queue_session_holds_only_failed_startup_and_preserves_status(self):
        for status in (0, 9):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bash = root / "bash"
                bash.write_text("#!/bin/sh\nexit " + str(status) + "\n")
                bash.chmod(0o700)
                result = subprocess.run(utility.queue_session_command(), env={"PATH": str(root)},
                                        input="\n", capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, status)
                self.assertEqual("Press Enter to close" in result.stderr, bool(status))

    def test_creation_reserves_stages_and_commits_an_ordinary_managed_queue_tab(self):
        for host in ("", "origin"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                (directory / "remote.json").write_text("{}")
                events = []

                def query(_directory, *args):
                    if args[0] == "launch":
                        events.append("launch")
                        self.assertEqual(events[:2], ["reserve", "launch"])
                        journal = json.loads((directory / "pending-create.json").read_text())
                        self.assertEqual(journal["initial_program"], "condor-queue")
                        self.assertIn("--stage-new", args)
                        self.assertIn("--initial-program", args)
                        self.assertIn("--tab-title=HTCondor Queue", args)
                        self.assertIn("--next-to=id:7", args)
                        self.assertIn("--var=kzw_workspace=Test", args)
                        self.assertNotIn("--var=kzw_utility=condor-queue", args)
                        return "8"
                    return json.dumps(tree(host, queue="launch" in events))

                with patch("kzw.cli.state_dir", return_value=directory), \
                     patch("kzw.cli.uuid.uuid4", return_value=SimpleNamespace(hex="b" * 32)), \
                     patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), \
                     patch("kzw.cli.capture_locked") as capture, \
                     patch("kzw.cli.view_query", side_effect=query), \
                     patch("kzw.cli.remote_client.renew_lease"), patch("kzw.cli.remote_client.ensure_current_revision"), \
                     patch("kzw.cli.remote_client.reserve_pane", side_effect=lambda *a: events.append("reserve")), \
                     patch("kzw.shell_cwd.from_session", return_value="/work/current"), \
                     patch("sys.stdout", new_callable=io.StringIO):
                    cli.create_pane("Test", "tab", 7, A, host, origin=host, initial_program="condor-queue")
                self.assertEqual(capture.call_args.kwargs["expected_panes"][7], (A, host))
                self.assertEqual(len(capture.call_args.kwargs["expected_panes"]), 2)
                self.assertFalse((directory / "pending-create.json").exists())

    def test_failed_queue_launch_retains_its_intent_without_replaying(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch("kzw.cli.state_dir", return_value=directory), \
                 patch("kzw.cli.shutil.which", return_value="/bin/kzw-pane"), patch("kzw.cli.capture_locked"), \
                 patch("kzw.shell_cwd.from_session", return_value=""), patch("kzw.cli.zmx_command", return_value=""), \
                 patch("kzw.cli.view_query", side_effect=[json.dumps(tree("")), OSError("lost launch reply")]) as query:
                with self.assertRaisesRegex(OSError, "lost launch reply"):
                    cli.create_pane("Test", "tab", 7, A, "", initial_program="condor-queue")
            self.assertEqual(query.call_count, 2)
            journal = json.loads((directory / "pending-create.json").read_text())
            self.assertEqual(journal["initial_program"], "condor-queue")
            self.assertEqual(journal["before"], [A])

    def test_startup_program_runs_inside_zmx_only_once_even_after_disconnect(self):
        for host in ("", "origin"):
            with self.subTest(host=host):
                env = {"KZW_WORKSPACE_COORDINATOR": "1", "KZW_WORKSPACE_ORIGIN": host,
                       "KZW_WORKSPACE_DIRECTORY": "/private/Test", "KZW_WORKSPACE": "Test"}
                events = []

                def attach(command, *args):
                    events.append("attach")
                    if events.count("attach") == 1:
                        self.assertIn("stage", events)
                        return subprocess.CompletedProcess(command, 255)
                    return subprocess.CompletedProcess(command, 0)

                with patch.dict(os.environ, env, clear=True), \
                     patch("sys.argv", ["kzw-pane", "--pane", B, "--host", host, "--stage-new",
                                        "--initial-program", "condor-queue", "--initial-cwd", "/a b'c"]), \
                     patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO), \
                     patch("kzw.pane.journal_new_workspace_pane"), patch("kzw.pane.verify_workspace_lease"), \
                     patch("kzw.pane.wait_for_native_stage", side_effect=lambda *a: events.append("stage")), \
                     patch("kzw.ssh_transport.pane_options", return_value=[]), \
                     patch("kzw.pane.run_attachment", side_effect=attach) as run, \
                     patch("kzw.pane.subprocess.run", return_value=subprocess.CompletedProcess([], 0, B + "\n")), \
                     patch("kzw.pane.report_attachment_ended"):
                    pane.main()
                self.assertEqual(events, ["stage", "attach", "attach"])
                first, second = [call.args[0] for call in run.call_args_list]
                if host:
                    self.assertIn("exec zmx attach " + B, first[-1])
                    self.assertIn("tmux_condor_queue.sh", first[-1])
                    self.assertNotIn("tmux_condor_queue.sh", second[-1])
                    self.assertIn("zmx list --short", second[-1])
                    self.assertIn("/a b'c", shlex.split(first[-1]))
                else:
                    self.assertEqual(first[:3], ["zmx", "attach", B])
                    self.assertIn("tmux_condor_queue.sh", shlex.join(first[3:]))
                    self.assertEqual(second, ["zmx", "attach", B])

    def test_existing_or_unstaged_panes_reject_initial_program(self):
        for extra in (["--existing", "--stage-new"], []):
            with self.subTest(extra=extra), patch("sys.argv", ["kzw-pane", "--pane", B,
                    "--initial-program", "condor-queue", *extra]), patch("sys.stderr", new_callable=io.StringIO), \
                    patch("kzw.pane.run_attachment") as run:
                with self.assertRaises(SystemExit):
                    pane.main()
                run.assert_not_called()

    def test_queue_is_saved_validated_and_listed_without_replaying_startup(self):
        exported = ("new_tab Work\nlaunch 'kitty-unserialize-data={\"id\":7}' /bin/kzw-pane --pane " + A +
                    "\nnew_tab HTCondor Queue\nlaunch 'kitty-unserialize-data={\"id\":8}' /bin/kzw-pane --pane " + B +
                    " --stage-new --initial-program condor-queue\nfocus_tab 1\n")
        saved = rewrite_export(exported, pane_map(tree("origin", True)), "/bin/kzw-pane")
        self.assertNotIn("--initial-program", saved)
        portable, manifest = remote_client.translate(saved, "/bin/kzw-pane", "origin", True)
        locations = layout_sessions(portable, manifest)
        self.assertEqual(locations[B]["tab"], 2)
        self.assertEqual(sessions.snapshot_tabs(portable, manifest), [[A], [B]])
        with tempfile.TemporaryDirectory() as tmp, patch("kzw.remote.subprocess.run", return_value=
                subprocess.CompletedProcess([], 0, A + "\n" + B + "\n")):
            store = Store(Path(tmp) / "state.sqlite")
            try:
                store.dispatch({"method": "workspace.put", "workspace": "Test", "revision": 0,
                                "request_id": "managed-queue", "snapshot": portable, "panes": manifest})
                state = store.dispatch({"method": "workspace.get", "workspace": "Test"})
                self.assertEqual(sessions.snapshot_tabs(state["snapshot"], state["panes"]), [[A], [B]])
                inventory = store.inventory()
                queue = next(entry for entry in inventory["sessions"] if entry["id"] == B)
                self.assertEqual(queue["workspace"], "Test")
            finally:
                store.close()
