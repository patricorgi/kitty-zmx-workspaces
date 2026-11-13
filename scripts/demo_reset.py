"""Destructively replace only the explicitly selected demo workspace."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kzw.core import PANE, atomic_write, check_name


class ResetError(RuntimeError):
    pass


class Reset:
    def __init__(self, name, host, run_dir, kitty, env):
        from kzw.cli import state_dir
        self.name, self.host = check_name(name), host
        if host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host):
            raise ResetError("invalid reset host")
        self.run_dir, self.kitty, self.env = Path(run_dir), kitty, dict(env)
        self.directory = state_dir(name, host)
        if self.directory.is_symlink():
            raise ResetError("workspace cache is a symlink; replacement refused")
        if self.directory.resolve() in self.run_dir.resolve().parents:
            raise ResetError("reset evidence must not be inside the workspace being removed")
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log = self.run_dir / "reset.log"
        self.progress = {"name": name, "host": host, "closed_views": [], "stopped_supervisors": []}

    def evidence(self, name, data):
        atomic_write(self.run_dir / name, json.dumps(data, indent=2) + "\n")

    def command(self, command, input=None, timeout=30):
        result = subprocess.run(command, env=self.env, input=input, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            with self.log.open("a") as log:
                log.write("\nFailed " + Path(command[0]).name + ":\n" + result.stdout + result.stderr)
            raise ResetError("demo reset command failed; see private reset.log")
        return result.stdout

    def remote(self, action, **values):
        request = {"name": self.name, "action": action,
                   "state_home": self.env.get("KZW_REMOTE_STATE_HOME", ""),
                   "authority": (ROOT / "kzw/remote.py").read_text(), **values}
        code = Path(__file__).with_name("demo_reset_remote.py").read_text()
        command = 'PATH="$HOME/.local/bin:$PATH"; export PATH; unset ZMX_SESSION ZMX_SESSION_PREFIX; exec ' + shlex.join(
            ["python3", "-u", "-c", code])
        return json.loads(self.command(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                       self.host, command], input=json.dumps(request), timeout=180))

    def raw(self, address, *args):
        return self.command([self.kitty, "@", "--to", address, *args], timeout=15)

    def local_metadata(self):
        references, previous_tokens = set(), []
        config = self.directory / "remote.json"
        if config.exists():
            data = json.loads(config.read_text())
            if (data.get("workspace"), data.get("host"), data.get("state_home", "")) != (
                    self.name, self.host, self.env.get("KZW_REMOTE_STATE_HOME", "")):
                raise ResetError("local reset cache has a different origin; nothing removed")
        for path in self.directory.iterdir():
            if path.is_symlink():
                raise ResetError("workspace cache contains a symlink; inspect it before reset")
            if path.is_file() and (path.suffix == ".json" or path.name.endswith("kitty-session")):
                references.update(re.findall(r"kzw-[0-9a-f]{32}", path.read_text()))
        pending = self.directory / "native-pending"
        if pending.is_dir():
            for path in pending.glob("*.json"):
                if path.is_symlink() or not path.is_file():
                    raise ResetError("unsafe native creation journal")
                data = json.loads(path.read_text())
                if (data.get("workspace"), data.get("host")) != (self.name, self.host):
                    raise ResetError("native creation journal has a different owner")
                references.add(data["pane"])
        lease = self.directory / "lease.json"
        if lease.is_file():
            data = json.loads(lease.read_text())
            if (data.get("workspace"), data.get("host"), data.get("state_home", "")) != (
                    self.name, self.host, self.env.get("KZW_REMOTE_STATE_HOME", "")):
                raise ResetError("lease has a different owner")
            previous_tokens = [data[key] for key in ("token", "recovery_token") if key in data]
        if any(not PANE.fullmatch(pane) for pane in references):
            raise ResetError("invalid locally referenced session")
        return references, previous_tokens

    def views(self, owned):
        addresses = {self.env["KITTY_LISTEN_ON"]} if self.env.get("KITTY_LISTEN_ON") else set()
        legacy = self.directory / "kitty.sock"
        # Shared attachments use an empty regular kitty.sock *marker* alongside
        # view.json, not a second Unix socket. Never try connecting to that file.
        if legacy.is_socket():
            addresses.add("unix:" + str(legacy))
        view = self.directory / "view.json"
        if view.is_file():
            addresses.add(json.loads(view.read_text())["address"])
        selected = []
        for address in addresses:
            for os_window in json.loads(self.raw(address, "ls")):
                for tab in os_window["tabs"]:
                    for window in tab["windows"]:
                        variables = window.get("user_vars", {})
                        if (variables.get("kzw_workspace"), variables.get("kzw_host")) != (self.name, self.host):
                            continue
                        if (variables.get("kzw_directory") != str(self.directory) or
                                variables.get("kzw_pane") not in owned):
                            raise ResetError("a matching demo view has unconfirmed ownership; nothing closed")
                        if address == self.env.get("KITTY_LISTEN_ON") and str(window["id"]) == self.env.get("KITTY_WINDOW_ID"):
                            raise ResetError("run demo replacement from a terminal outside the demo being removed")
                        selected.append({"address": address, "os_id": os_window["id"], "id": window["id"],
                                         "pid": window["pid"], "pane": variables["kzw_pane"]})
        return selected

    def close_views(self, selected):
        for target in selected:
            tree = json.loads(self.raw(target["address"], "ls"))
            matches = [window for osw in tree if osw["id"] == target["os_id"]
                       for tab in osw["tabs"] for window in tab["windows"] if window["id"] == target["id"]]
            if len(matches) != 1 or matches[0]["pid"] != target["pid"]:
                raise ResetError("demo pane identity changed before close")
            variables = matches[0].get("user_vars", {})
            if (variables.get("kzw_pane"), variables.get("kzw_workspace"), variables.get("kzw_host"),
                    variables.get("kzw_directory")) != (target["pane"], self.name, self.host, str(self.directory)):
                raise ResetError("demo pane ownership changed before close")
            self.progress["closed_views"].append(target)
            self.evidence("reset-progress.json", self.progress)
            self.raw(target["address"], "close-window", "--match", "id:" + str(target["id"]))

    def process_identity(self, pid):
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                env=self.env, capture_output=True, text=True, timeout=10)
        if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
            return None
        if result.returncode:
            raise ResetError("cannot inspect demo supervisor identity")
        return result.stdout.strip()

    def stop_supervisors(self):
        lock = self.directory / "lock"
        if not lock.exists():
            return
        result = subprocess.run(["lsof", "-t", str(lock)], env=self.env, capture_output=True, text=True, timeout=10)
        if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr.strip()):
            raise ResetError("cannot identify demo lock owners")
        for text in result.stdout.split():
            pid = int(text)
            if pid == os.getpid():
                continue
            identity = self.process_identity(pid)
            if identity is None:
                continue
            # ps starts with five lstart fields. Parse the command, not substrings
            # of another workspace's name or shell command.
            args = shlex.split(identity.split(None, 5)[5])
            if "-m" not in args:
                raise ResetError("unrecognized demo lock owner; not terminated")
            args = args[args.index("-m") + 1:]
            host_index = args.index("--host") if "--host" in args else -1
            if (len(args) < 3 or args[0] != "kzw.cli" or args[1] not in ("create", "attach", "start", "new-pane") or
                    args[2] != self.name or host_index < 0 or host_index + 1 >= len(args) or args[host_index + 1] != self.host):
                raise ResetError("demo lock owner does not match the selected workspace")
            current = self.process_identity(pid)
            if current is None:
                continue
            if current != identity:
                raise ResetError("supervisor PID identity changed; not terminated")
            self.progress["stopped_supervisors"].append({"pid": pid, "identity": identity})
            self.evidence("reset-progress.json", self.progress)
            os.kill(pid, signal.SIGTERM)

    def execute(self):
        stable = self.directory.parent / ("." + self.name + ".demo-reset.lock")
        with stable.open("a+") as serial:
            fcntl.flock(serial, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.remote("inspect")
            self.evidence("reset-before.json", before)
            references, previous_tokens = self.local_metadata()
            if type(before.get("exists")) is not bool or not before["inventory"]["available"]:
                raise ResetError("workspace absence or inventory is unknown; nothing removed")
            owned = set(before["owned"])
            rows = {row["id"]: row for row in before["inventory"]["sessions"]}
            for pane in references - owned:
                row = rows.get(pane)
                if row and (row["references"] or row["presence"] != "missing"):
                    raise ResetError("a cached session is live/unassigned or has another owner; inspect before reset")
            selected = self.views(owned)
            intent_path = self.directory / "demo-reset.intent.json"
            if intent_path.exists():
                intent = json.loads(intent_path.read_text())
                if (intent.get("name"), intent.get("host")) != (self.name, self.host):
                    raise ResetError("reset journal has a different target")
            else:
                intent = {"name": self.name, "host": self.host,
                          "token": secrets.token_hex(16), "request_id": secrets.token_hex(16)}
                atomic_write(intent_path, json.dumps(intent) + "\n")
            self.close_views(selected)
            self.stop_supervisors()
            with (self.directory / "lock").open("a+") as lock:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise ResetError("demo supervisor did not stop; state retained")
                        time.sleep(.1)
                # Read ownership again after stopping the local clients. The
                # authority revalidates revision, claims, lease and namespace too.
                current = self.remote("inspect")
                if set(current["owned"]) != owned or current["revision"] != before["revision"]:
                    raise ResetError("demo changed while closing its views; state retained for retry")
                result = self.remote("destroy", revision=current["revision"], owned=sorted(owned),
                                     token=intent["token"], request_id=intent["request_id"],
                                     previous_tokens=previous_tokens)
                self.evidence("reset-after.json", result)
                if not result.get("removed"):
                    raise ResetError("remote demo removal is not confirmed")
                # Preserve every interrupted journal/cache instead of recursive
                # deletion. A fresh kzw create gets a genuinely empty directory.
                archive = self.run_dir / "previous-workspace"
                self.directory.rename(archive)
                return result


def reset_workspace(name, host, run_dir, kitty, env):
    return Reset(name, host, run_dir, kitty, env).execute()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Demo")
    parser.add_argument("--host", default="example-host")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--yes", action="store_true", required=True, help="explicitly authorize ending this workspace's processes")
    args = parser.parse_args()
    import shutil
    result = reset_workspace(args.name, args.host, args.run_dir, shutil.which("kitty"), os.environ)
    print(json.dumps({"removed": result["removed"], "sessions": len(result["sessions"])}))


if __name__ == "__main__":
    main()
