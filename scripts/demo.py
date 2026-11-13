#!/usr/bin/env python3
"""Replace the selected workspace, run the remote Kitty demo, then end it."""

import argparse
import base64
import fcntl
import json
import math
import os
from pathlib import Path
import re
import runpy
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kzw.core import check_name

EXPECTED = {"matrix": "cmatrix", "clock": "bash", "monitor": "htop", "files": "yazi"}


class DemoError(RuntimeError):
    pass


class CommandError(DemoError):
    def __init__(self, command, result):
        self.command, self.stderr = command, result.stderr
        super().__init__(Path(command[0]).name + " failed; details are in the private diagnostics log")


class WindowError(DemoError):
    def __init__(self, code):
        self.code = code
        messages = {
            "no-display": "no macOS GUI display is available; run from a logged-in desktop",
            "accessibility-required": "enable Accessibility for the terminal running this demo in System Settings > Privacy & Security > Accessibility",
            "automation-required": "allow the terminal to control System Events in System Settings > Privacy & Security > Automation",
            "process-not-ready": "the demo Kitty process is not yet visible to macOS Accessibility",
            "window-not-ready": "the exact demo window title is not yet visible to macOS Accessibility",
            "ambiguous-process": "the demo process identity is ambiguous; no window was changed",
            "ambiguous-window": "the demo window title is ambiguous; no window was changed",
            "geometry-rejected": "the window manager did not accept the requested demo geometry",
            "unknown-operation": "unsupported demo window operation",
        }
        super().__init__(messages.get(code, "the macOS window helper returned an unknown error"))


def centered_frame(screen, area):
    scale = math.sqrt(area)
    width, height = round(screen["width"] * scale), round(screen["height"] * scale)
    return {"x": round(screen["x"] + (screen["width"] - width) / 2),
            "y": round(screen["y"] + (screen["height"] - height) / 2),
            "width": width, "height": height}


def pane_order(state):
    return [[pane["pane"] for pane in tab["panes"]] for tab in state["tabs"]]


def focus(state):
    return next(pane["pane"] for tab in state["tabs"] if tab["active"]
                for pane in tab["panes"] if pane["active"])


def normalized_rects(state):
    result = {}
    for tab in state["tabs"]:
        rects = [pane["rect"] for pane in tab["panes"]]
        left, top = min(r[0] for r in rects), min(r[1] for r in rects)
        width, height = max(r[2] for r in rects) - left, max(r[3] for r in rects) - top
        for pane in tab["panes"]:
            x1, y1, x2, y2 = pane["rect"]
            result[pane["pane"]] = [(x1 - left) / width, (y1 - top) / height,
                                    (x2 - left) / width, (y2 - top) / height]
    return result


def check_restored(before, after):
    if pane_order(before) != pane_order(after) or focus(before) != focus(after):
        raise DemoError("tab/pane ordering or focus was not restored")
    old, new = normalized_rects(before), normalized_rects(after)
    if any(abs(a - b) > .03 for pane in old for a, b in zip(old[pane], new[pane])):
        raise DemoError("split proportions were not restored")


def check_bsp(state, roles):
    if [len(tab["panes"]) for tab in state["tabs"]] != [1, 3]:
        raise DemoError("expected one startup pane and three panes in the second tab")
    if state["tabs"][1]["layout"] != "splits":
        raise DemoError("the demo tab is not in Kitty's BSP splits layout")
    panes = {pane["pane"]: pane["rect"] for pane in state["tabs"][1]["panes"]}
    left, top, bottom = [panes[roles[role]] for role in ("clock", "monitor", "files")]
    tolerance = 32  # Window padding/borders and cell rounding, in render pixels.
    if not (left[0] < top[0] and top[1] < bottom[1] and left[2] <= top[0] and
            top[3] <= bottom[1] and abs(left[1] - top[1]) <= tolerance and
            abs(left[3] - bottom[3]) <= tolerance and abs(top[0] - bottom[0]) <= tolerance and
            abs(top[2] - bottom[2]) <= tolerance):
        raise DemoError("the second tab does not have the requested left/top-right/bottom-right topology")


def programs(tools, target):
    matrix = ("exec " + shlex.join([tools["cmatrix"], "-u", "8"]) if tools.get("cmatrix") else
              "sleep_command=" + shlex.quote(tools["sleep"]) + "\n" +
              (SCRIPTS / "demo_rain.sh").read_text())
    return {"matrix": matrix,
            "clock": "while :; do " + shlex.quote(tools["date"]) + "; " +
                     shlex.join([tools["sleep"], "2"]) + "; done",
            "monitor": "exec " + shlex.join([tools["htop"], "-d", "20"]),
            "files": "exec " + shlex.join([tools["yazi"], target])}


def startup_command(tools, token, role, program):
    # Never paste a multiline shell program into readline. Encode both the Bash
    # path and program, then replace a tiny Python bootstrap with the same Bash
    # process that our PID/start-time verifier expects.
    payload = base64.b64encode(json.dumps([tools["bash"], program]).encode()).decode("ascii")
    bootstrap = ("import base64,json,os; b,p=json.loads(base64.b64decode(" + repr(payload) +
                 ")); os.execv(b,[b,'--noprofile','--norc','-c',p])")
    command = shlex.join([tools["env"], "KZW_DEMO_TOKEN=" + token, "KZW_DEMO_ROLE=" + role,
                          tools["python3"], "-c", bootstrap])
    if "\n" in command or "\r" in command:
        raise DemoError("startup launcher must be one physical terminal line")
    return command


def probe_error(error):
    """Explain known prerequisite failures without echoing arbitrary remote text."""
    code = error.get("code") if isinstance(error, dict) else None
    messages = {"unsupported-origin": "the demo origin needs Linux and Python 3.8+",
                "missing-directory": "remote ~/dotfiles/.config/nvim does not exist"}
    if code in messages:
        return messages[code]
    names = error.get("programs") if isinstance(error, dict) else None
    allowed = {"python3", "bash", "env", "date", "sleep", "zmx", "htop", "yazi", "cmatrix"}
    if isinstance(names, list) and names and all(isinstance(name, str) and name in allowed for name in names):
        if code == "missing-programs":
            return "missing remote programs: " + ", ".join(names)
        if code == "version-check":
            return "cannot check remote program versions: " + ", ".join(names)
    return "remote prerequisite check failed; see the private diagnostics log"


class Demo:
    def __init__(self, args):
        self.args = args
        self.token = secrets.token_hex(12)
        self.title = "KZW Demo " + self.token
        self.env = os.environ.copy()
        self.env.pop("KZW_TEST_KITTY_BACKGROUND", None)
        self.env.pop("KZW_BACKGROUND_SUPERVISOR", None)
        self.env.pop("KZW_STARTUP_STATUS", None)
        self.env["PATH"] = str(ROOT / "bin") + os.pathsep + self.env.get("PATH", "")
        self.env["PYTHONPATH"] = str(ROOT) + os.pathsep + self.env.get("PYTHONPATH", "")
        self.kitty = shutil.which("kitty")
        self.address = None
        self.anchor = None
        self.os_id = None
        self.directory = None
        self.shared = None
        state = Path(self.env.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.run_dir = state / "kzw/demo-runs" / self.token
        self.run_dir.mkdir(parents=True, mode=0o700)
        self.run_dir.chmod(0o700)
        self.log_path = self.run_dir / "diagnostics.log"
        self.log_path.touch(mode=0o600)

    def note(self, message):
        print("demo: " + message, flush=True)

    def run(self, command, timeout=30, input=None):
        try:
            source = {"stdin": subprocess.DEVNULL} if input is None else {"input": input}
            result = subprocess.run(command, env=self.env, cwd=ROOT, **source,
                                    capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DemoError("could not run " + Path(command[0]).name) from exc
        if result.returncode:
            # Never echo remote output or terminal contents into the recording.
            with self.log_path.open("a") as log:
                log.write("\nFailed: " + Path(command[0]).name + "\n" + result.stdout + result.stderr)
            raise CommandError(command, result)
        return result.stdout

    def kzw(self, *args, input=None):
        try:
            source = {} if input is None else {"input": input}
            return self.run([sys.executable, "-m", "kzw.cli", *args], timeout=180, **source)
        except DemoError:
            # A supervisor can still be alive after its startup caller times out.
            # Retain its explanation, but never stop it or retry program creation.
            if self.directory:
                log = self.directory / "supervisor.log"
                if log.is_file() and not log.is_symlink():
                    with self.log_path.open("a") as diagnostics:
                        diagnostics.write("\nWorkspace supervisor log:\n" + log.read_text()[-16000:] + "\n")
            raise

    def remote(self, mode, **values):
        request = json.dumps({"mode": mode, **values})
        script = (SCRIPTS / "demo_remote.py").read_text()
        command = 'PATH="$HOME/.local/bin:$PATH"; export PATH; exec ' + shlex.join(
            ["python3", "-c", script, request])
        output = self.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                           self.args.host, command], timeout=45)
        result = json.loads(output)
        if "error" in result:
            with self.log_path.open("a") as log:
                log.write("\nRemote probe error:\n" + output + "\n")
            raise DemoError(probe_error(result["error"]))
        return result

    def window(self, *args):
        result = json.loads(self.run(["/usr/bin/osascript", "-l", "JavaScript",
                                      str(SCRIPTS / "demo_window.js"), *args]))
        if "error" in result:
            error = result["error"]
            raise WindowError(error.get("code") if isinstance(error, dict) else None)
        return result

    def rc(self, *args, input=None):
        if not self.address:
            raise DemoError("no demo remote-control socket")
        source = {} if input is None else {"input": input}
        return self.run([self.kitty, "@", "--to", self.address, *args], timeout=20, **source)

    def wait(self, description, predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.25)
        raise DemoError("timed out waiting for " + description)

    def preflight(self):
        if sys.platform != "darwin":
            raise DemoError("this demo's centered-window driver currently requires macOS")
        for name in ("kitty", "ssh", "osascript"):
            if not shutil.which(name):
                raise DemoError("missing local tool: " + name)
        from kzw import kitty_instance
        try:
            self.shared = kitty_instance.context(self.env)
            kitty_instance.probe(self.shared["address"])
        except ValueError as error:
            raise DemoError("run the demo inside your existing single-instance Kitty with remote control enabled: " +
                            str(error)) from error
        marker = self.args.marker.expanduser().resolve()
        if not marker.is_file():
            raise DemoError("your privacy marker was not found")
        # Validate rules without printing the private list or matching real secrets.
        marker_fn = runpy.run_path(str(marker))["marker"]
        if not any(start <= 0 and end >= 8 and group == 1 for start, end, group in marker_fn("192.0.2.1")):
            raise DemoError("privacy marker failed the synthetic IP masking check")
        self.marker = str(marker)
        # Matching foreground/background is essential; a colored highlight isn't redaction.
        local = marker.parent.parent / "local.conf"
        colors = dict(re.findall(r"^\s*(mark1_(?:foreground|background))\s+(#[0-9a-fA-F]{6})\s*$",
                                 local.read_text(), re.MULTILINE))
        if (len(colors) != 2 or colors["mark1_foreground"].lower() != colors["mark1_background"].lower()):
            raise DemoError("local.conf must configure identical mark1 foreground/background colors")
        self.mask_color = colors["mark1_background"]
        self.note("checking SSH and remote dependencies (nothing is installed automatically)")
        probe = self.remote("preflight")
        (self.run_dir / "preflight.json").write_text(json.dumps(probe, indent=2))
        self.tools = probe["tools"]
        self.commands = programs(self.tools, probe["target"])
        self.expected = dict(EXPECTED)
        self.startup_label = "cmatrix"
        if not self.tools.get("cmatrix"):
            self.expected["matrix"] = "bash"
            self.startup_label = "built-in Matrix-style animation"
            self.note("cmatrix is unavailable; using a built-in Bash animation (no installation needed)")
        from kzw import remote_client
        # An unavailable query is an error, never evidence that Demo is free.
        existing = remote_client.call(self.args.host, {"method": "workspace.sync", "workspace": self.args.name})
        if type(existing.get("exists")) is not bool:
            raise DemoError("workspace existence is unconfirmed; replacement refused")
        self.existing = existing["exists"]
        if self.existing:
            self.note("the existing selected workspace will be terminated and replaced (except with --check)")
        self.frame = centered_frame(self.window("screen"), self.args.area)
        self.write_launcher()

    def write_launcher(self):
        # Hide only the forwarded new OS window; do not start another macOS app
        # instance or change the source instance's options, shell or shortcuts.
        bindir = self.run_dir / "bin"
        bindir.mkdir(mode=0o700)
        wrapper = bindir / "kitty"
        wrapper.write_text("#!/bin/sh\nset -eu\nif [ \"${1-}\" = --single-instance ]; then\n"
                           "  exec " + shlex.quote(self.kitty) + " --start-as=hidden \"$@\"\nfi\nexec " +
                           shlex.quote(self.kitty) + " \"$@\"\n")
        wrapper.chmod(0o700)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env["PATH"]
        return wrapper

    def reset_existing(self):
        self.note("removing any existing selected demo and its owned sessions before starting fresh")
        namespace = runpy.run_path(str(SCRIPTS / "demo_reset.py"))
        try:
            return namespace["reset_workspace"](self.args.name, self.args.host, self.run_dir / "reset", self.kitty, self.env)
        except namespace["ResetError"] as error:
            raise DemoError("demo replacement stopped: " + str(error)) from error
        except Exception as error:
            with self.log_path.open("a") as diagnostics:
                diagnostics.write("\nDemo replacement failed:\n" + traceback.format_exc())
            raise DemoError("demo replacement did not finish; state retained. See the private reset diagnostics") from error

    def discover(self, expected_count):
        tree = json.loads(self.rc("ls"))
        matches = [window for window in tree if window.get("id") == self.os_id]
        if len(matches) != 1:
            raise DemoError("the recorded demo OS window is missing or ambiguous")
        os_window = matches[0]
        windows = [window for tab in os_window["tabs"] for window in tab["windows"]]
        if len(windows) != expected_count:
            raise DemoError("the demo pane count changed unexpectedly")
        if any(window.get("user_vars", {}).get("kzw_workspace") != self.args.name or
               window.get("user_vars", {}).get("kzw_host") != self.args.host for window in windows):
            raise DemoError("refusing to operate on a foreign window")
        self.os_id, self.anchor = os_window["id"], windows[0]["id"]
        return windows

    def mask(self, count):
        windows = self.discover(count)
        request = {"name": self.args.name, "host": self.args.host, "os_window_id": self.os_id,
                   "windows": [window["id"] for window in windows], "marker": self.marker}
        state = json.loads(self.rc("kitten", "--match", "id:" + str(self.anchor),
                                   str(SCRIPTS / "demo_kitten.py"), json.dumps(request)))
        for window in windows:
            colors = dict(line.split() for line in self.rc(
                "get-colors", "--match", "id:" + str(window["id"])).splitlines() if line.strip())
            if any(colors.get(key, "").lower() != self.mask_color.lower()
                   for key in ("mark1_foreground", "mark1_background")):
                raise DemoError("a pane's marker colors are not the expected masking colors")
            self.rc("set-window-title", "--match", "id:" + str(window["id"]), self.title)
        return state

    def show(self, count):
        state = self.mask(count)
        self.rc("resize-os-window", "--match", "id:" + str(self.anchor), "--action=show")
        def place():
            try:
                return self.window("place", str(state["pid"]), self.title, json.dumps(self.frame))
            except WindowError as error:
                # AX can briefly lag Kitty's successful show response.
                if error.code not in ("process-not-ready", "window-not-ready"):
                    raise
                return None
        self.wait("the centered demo window (check Accessibility permission)", place, timeout=12)
        return self.mask(count)

    def hide(self):
        self.rc("resize-os-window", "--match", "id:" + str(self.anchor), "--action=hide")

    def attach(self, create=False):
        from kzw.cli import state_dir, view_state
        self.directory = state_dir(self.args.name, self.args.host)
        self.address, self.anchor, self.os_id = None, None, None
        if create:
            # Create before starting the background supervisor, so a retired-cache
            # archive cannot move the newly opened supervisor log out of sight.
            self.kzw("create", self.args.name, "--host", self.args.host)
        self.kzw("attach", self.args.name, "--host", self.args.host)
        view = view_state(self.directory)
        if (view is None or view["address"] != self.shared["address"] or
                view["instance_pid"] != self.shared["pid"]):
            raise DemoError("attachment did not record the expected scoped Kitty view; state retained")
        self.address, self.os_id = view["address"], view["os_window_id"]

    def new_pane(self, source, kind, count):
        before = {window["user_vars"]["kzw_pane"] for window in self.discover(count)}
        # The current CLI maps 'right' to hsplit (top/bottom) and 'below' to
        # vsplit (left/right). Compensate here, without changing user shortcuts.
        managed_kind = {"right": "below", "below": "right"}.get(kind, kind)
        for attempt in range(8):
            try:
                self.kzw("_managed-create", self.args.name, managed_kind, str(source["id"]),
                         source["user_vars"]["kzw_pane"], "--host", self.args.host, "--origin", self.args.host)
                break
            except CommandError as error:
                # The CLI can reject its initial snapshot during a title/resize
                # transition. Retry only this exact pre-mutation failure, never
                # a launched pane, pending operation or uncertain SSH outcome.
                if error.stderr.strip() != "kzw: layout changed while taking snapshot" or attempt == 7:
                    raise
                pending = ("pending-create.json", "pending-close.json", "pending-exit.json", "native-stage.json")
                if any((self.directory / name).exists() for name in pending):
                    raise
                config = self.directory / "remote.json"
                if config.is_file() and json.loads(config.read_text()).get("pending"):
                    raise
                current = {window["user_vars"]["kzw_pane"] for window in self.discover(count)}
                if current != before:
                    raise
                time.sleep(.2)
        self.mask(count + 1)
        windows = self.discover(count + 1)
        added = [window for window in windows if window["user_vars"]["kzw_pane"] not in before]
        if len(added) != 1:
            raise DemoError("managed creation did not add exactly one pane")
        return added[0]

    def start(self, window, role):
        command = startup_command(self.tools, self.token, role, self.commands[role])
        # stdin is literal, unlike send-text positional arguments. Paste the
        # command using the terminal's bracketed-paste mode, then submit Enter
        # outside the paste so readline cannot leave it waiting for execution.
        self.rc("send-text", "--match", "id:" + str(window["id"]),
                "--bracketed-paste=auto", "--stdin", input=command)
        self.rc("send-text", "--match", "id:" + str(window["id"]), "--stdin", input="\r")

    def process_state(self):
        return self.remote("processes", token=self.token, expected=self.expected)

    def ready(self, roles):
        state = self.process_state()
        return state if set(roles).issubset(state["processes"]) else None

    def detached(self):
        if (self.directory / "kitty.sock").exists() or (self.directory / "view.json").exists():
            return False
        with (self.directory / "lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        return True

    def ended(self):
        from kzw import remote_client
        state = remote_client.call(self.args.host, {"method": "workspace.sync", "workspace": self.args.name})
        if type(state.get("exists")) is not bool:
            raise DemoError("demo termination is unconfirmed; state retained")
        return not state["exists"]

    def finish(self):
        self.note("showing the final scene for 10 more seconds before ending the demo")
        time.sleep(10)
        # Detach first so the normal CLI can acquire the released lease. Never
        # force-close clients or edit coordinator state if this handoff fails.
        self.kzw("detach", self.args.name, "--host", self.args.host)
        self.wait("final supervisor shutdown and lease release", self.detached)
        self.address = None
        # A normal demo run explicitly authorizes ending this selected workspace.
        self.kzw("kill", self.args.name, "--host", self.args.host, input="y\n")
        self.wait("confirmed demo workspace termination", self.ended)
        self.note("demo workspace ended; run files: " + str(self.run_dir))

    def execute(self):
        self.preflight()
        if self.args.check:
            self.note("preflight passed; no demo workspace was created")
            return
        self.reset_existing()
        self.note("creating the remote workspace in a hidden, scoped window of your current Kitty")
        self.attach(create=True)
        self.note("workspace attached; enabling privacy masking before program startup")
        self.mask(1)
        matrix = self.discover(1)[0]
        self.note("starting the startup animation")
        self.start(matrix, "matrix")
        self.wait("the startup animation", lambda: self.ready(["matrix"]))
        self.show(1)
        self.note("startup tab: " + self.startup_label)
        time.sleep(self.args.pause)

        # Do not display new shell prompts while their marker is initializing.
        self.hide()
        clock = self.new_pane(matrix, "tab", 1)
        self.rc("goto-layout", "--match", "window_id:" + str(clock["id"]), "splits")
        monitor = self.new_pane(clock, "right", 2)
        files = self.new_pane(monitor, "below", 3)
        roles = {role: window["user_vars"]["kzw_pane"]
                 for role, window in (("matrix", matrix), ("clock", clock), ("monitor", monitor), ("files", files))}
        for role, window in (("clock", clock), ("monitor", monitor), ("files", files)):
            self.start(window, role)
        baseline = self.wait("the four demo programs", lambda: self.ready(EXPECTED))
        self.rc("focus-window", "--match", "id:" + str(files["id"]))
        before = self.show(4)
        check_bsp(before, roles)
        self.note("second tab: date every 2s | htop above Yazi")
        time.sleep(self.args.pause)
        # detach already captures and acknowledges the final complete layout.
        # Do not issue a redundant `kzw save`: the current CLI's appearance
        # import shadows its capture function in that separate command path.
        before = self.mask(4)
        self.note("detaching; remote programs stay running")
        self.kzw("detach", self.args.name, "--host", self.args.host)
        self.wait("supervisor shutdown and lease release", self.detached)
        self.address = None
        time.sleep(self.args.detach_pause)
        if self.process_state() != baseline:
            raise DemoError("a demo program changed identity while detached; nothing will be recreated")

        self.note("reattaching the same sessions, without replaying program commands")
        self.attach()
        # Match the original OS-window size before comparing the pane geometry.
        after = self.show(4)
        check_bsp(after, roles)
        check_restored(before, after)
        if self.process_state() != baseline:
            raise DemoError("a demo program changed identity after reattachment")
        (self.run_dir / "proof.json").write_text(json.dumps(
            {"processes": baseline, "before": before, "after": after, "roles": roles}, indent=2))
        time.sleep(self.args.pause)
        restored = {window["user_vars"]["kzw_pane"]: window for window in self.discover(4)}
        self.rc("focus-window", "--match", "id:" + str(restored[roles["matrix"]]["id"]))
        time.sleep(self.args.pause)
        self.rc("focus-window", "--match", "id:" + str(restored[roles["files"]]["id"]))
        self.note("done: same four program PIDs/start times and restored BSP topology, tab order and focus")
        self.finish()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", default="example-host")
    result.add_argument("--name", default="Demo", help="DESTRUCTIVE: terminate and replace this workspace (default: Demo)")
    result.add_argument("--area", type=float, default=0.90, help="fraction of usable display AREA (default: .90)")
    result.add_argument("--pause", type=float, default=6, help="seconds to show each scene")
    result.add_argument("--detach-pause", type=float, default=3)
    result.add_argument("--marker", type=Path,
                        default=Path.home() / ".config/kitty/helpers/privacy_marker.py")
    result.add_argument("--check", action="store_true", help="preflight only; do not remove or create a workspace")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    check_name(args.name)
    if args.host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.host):
        raise DemoError("invalid SSH host")
    if not math.isfinite(args.area) or not 0 < args.area <= 1:
        raise DemoError("--area must be greater than 0 and at most 1")
    if any(not math.isfinite(value) or value < 0 for value in (args.pause, args.detach_pause)):
        raise DemoError("scene pauses must be finite and nonnegative")
    demo = Demo(args)
    try:
        demo.execute()
    except (Exception, KeyboardInterrupt):
        # Preserve all processes and journals. Hide only the verified demo view.
        if demo.address and demo.anchor:
            try:
                demo.hide()
            except Exception:
                pass
        print("demo: stopped; no additional cleanup attempted. Private diagnostics: " + str(demo.log_path), file=sys.stderr)
        raise
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DemoError, ValueError, OSError, subprocess.SubprocessError) as error:
        print("demo: " + str(error), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
