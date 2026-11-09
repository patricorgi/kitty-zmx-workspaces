"""Experimental single-writer remote workspace snapshots over OpenSSH."""

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path

from .core import PANE, atomic_write, check_name

DORMANT = "# kzw-dormant-workspace\n"
ORIGIN = "KZW_ORIGIN"
EXECUTABLE = "KZW_PANE_EXECUTABLE"


def call(host, request):
    if not isinstance(host, str) or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host):
        raise ValueError("invalid SSH host")
    # Ship our stdlib-only RPC handler over SSH; no remote install or TCP
    # listener is required. Remote state is persistent in its XDG state dir.
    source = Path(__file__).with_name("remote.py").read_bytes()
    program = "import base64;exec(base64.b64decode(%r))" % base64.b64encode(source).decode()
    state_override = os.environ.get("KZW_REMOTE_STATE_HOME", "")
    if state_override and (not state_override.startswith("/") or "\x00" in state_override):
        raise ValueError("KZW_REMOTE_STATE_HOME must be an absolute remote path")
    command = ("XDG_STATE_HOME=" + shlex.quote(state_override) + " " if state_override else "") + "python3 -u -c " + shlex.quote(program)
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                          input=json.dumps(request, separators=(",", ":")) + "\n", text=True,
                          capture_output=True, timeout=30, check=True)
    try:
        response = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid remote coordinator response") from exc
    if not response.get("ok"):
        raise ValueError("remote coordinator: " + str(response.get("error", "unknown error")))
    return response["result"]


def translate(snapshot, executable, host, portable):
    """Rewrite managed launch commands without replaying arbitrary programs."""
    if snapshot in (DORMANT, "# kzw-empty-workspace\n"):
        return snapshot, []
    lines, found = [], []
    for line in snapshot.splitlines():
        if line.startswith("cd "):
            # Local client paths are not portable; live shells retain cwd.
            continue
        if not line.startswith("launch "):
            lines.append(line)
            continue
        args = shlex.split(line)
        if len(args) < 7 or not args[1].startswith("kitty-unserialize-data="):
            raise ValueError("unsupported remote workspace launch")
        # Coordinator-generated commands end with executable --pane ID --host HOST --existing.
        command = args[-6:]
        if command[1] != "--pane" or command[3] != "--host" or command[5] != "--existing" or not PANE.fullmatch(command[2]):
            raise ValueError("remote workspace contains an unmanaged launch")
        if portable and command[4] != host:
            raise ValueError("remote workspace may only contain panes on its origin host")
        if not portable and (command[0] != EXECUTABLE or command[4] != ORIGIN):
            raise ValueError("remote workspace has invalid portable launch")
        args = args[:-6]
        args = [arg for arg in args if not arg.startswith("--cwd=") and not arg.startswith("--var=kzw_host=")]
        args.append("--var=kzw_host=" + (ORIGIN if portable else host))
        args.extend([EXECUTABLE if portable else executable, "--pane", command[2], "--host",
                     ORIGIN if portable else host, "--existing"])
        found.append(command[2])
        lines.append(shlex.join(args))
    if not found or len(found) != len(set(found)):
        raise ValueError("remote workspace has no managed panes or duplicate pane IDs")
    return "\n".join(lines) + "\n", sorted(found)


def snapshot_hash(snapshot, panes):
    payload = json.dumps([snapshot, sorted(panes)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def save_config(directory, config):
    atomic_write(directory / "remote.json", json.dumps(config, separators=(",", ":")) + "\n")


def publish(directory, snapshot, panes, confirmed_exits=False):
    """Publish with durable replay of requests whose response was lost."""
    config_path = directory / "remote.json"
    if not config_path.exists():
        return
    config = json.loads(config_path.read_text())
    host, name = config["host"], check_name(config["workspace"])
    if config.get("state_home", "") != os.environ.get("KZW_REMOTE_STATE_HOME", ""):
        raise ValueError("remote state directory differs from this workspace's origin")
    portable, parsed = translate(snapshot, "", host, True)
    if sorted(panes) != parsed:
        raise ValueError("snapshot and remote pane manifest disagree")
    desired = snapshot_hash(portable, parsed)
    while True:
        pending = config.get("pending")
        if pending:
            response = call(host, pending["request"])
            config["revision"] = response["revision"]
            config["published_hash"] = pending["hash"]
            config["panes"] = pending["request"]["panes"]
            config.pop("pending", None)
            save_config(directory, config)
        if config.get("published_hash") == desired:
            return
        old_panes = set(config.get("panes", []))
        removed = old_panes - set(parsed)
        if removed and not confirmed_exits:
            raise ValueError("remote pane removal requires a confirmed shell exit")
        request = {"method": "workspace.prune" if removed else "workspace.put", "workspace": name, "revision": config["revision"],
                   "request_id": uuid.uuid4().hex, "snapshot": portable, "panes": parsed}
        if removed:
            request["remove"] = sorted(removed)
        config["pending"] = {"request": request, "hash": desired}
        save_config(directory, config)


def create(directory, name, host):
    check_name(name)
    config_path = directory / "remote.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        if (config.get("workspace") != name or config.get("host") != host or not config.get("creating")
                or config.get("state_home", "") != os.environ.get("KZW_REMOTE_STATE_HOME", "")):
            raise ValueError("local workspace already exists; refusing to overwrite it")
    else:
        if any(p.name != "lock" for p in directory.iterdir()):
            raise ValueError("local workspace already exists; refusing to overwrite it")
        if call(host, {"method": "workspace.get", "workspace": name})["exists"]:
            raise ValueError("remote workspace already exists; use attach instead")
        config = {"workspace": name, "host": host, "revision": 0, "creating": True,
                  "state_home": os.environ.get("KZW_REMOTE_STATE_HOME", "")}
        save_config(directory, config)
    publish(directory, DORMANT, [])
    config = json.loads(config_path.read_text())
    config.pop("creating", None)
    save_config(directory, config)


def pull(directory, name, host, executable):
    check_name(name)
    path = directory / "remote.json"
    if path.exists():
        config = json.loads(path.read_text())
        if (config.get("host") != host or config.get("workspace") != name or config.get("pending") or config.get("creating")
                or config.get("state_home", "") != os.environ.get("KZW_REMOTE_STATE_HOME", "")):
            raise ValueError("local workspace has conflicting or pending remote state")
    elif any(directory.iterdir()):
        # The workspace lock is created by the caller before this check.
        if any(p.name != "lock" for p in directory.iterdir()):
            raise ValueError("local workspace already exists; refusing to overwrite it")
    remote = call(host, {"method": "workspace.get", "workspace": name})
    if not remote["exists"]:
        raise ValueError("remote workspace does not exist")
    snapshot, panes = translate(remote["snapshot"], executable, host, False)
    if panes != sorted(remote["panes"]):
        raise ValueError("remote snapshot and pane manifest disagree")
    # A dormant record has no pre-existing layout. A first attachment starts a
    # managed shell and publishes its snapshot once Kitty has created it.
    if snapshot != DORMANT:
        atomic_write(directory / "workspace.kitty-session", snapshot)
        atomic_write(directory / "panes.json", json.dumps(panes) + "\n")
    elif (directory / "workspace.kitty-session").exists():
        raise ValueError("dormant remote state conflicts with a local snapshot")
    save_config(directory, {"workspace": name, "host": host, "revision": remote["revision"], "panes": panes,
                            "state_home": os.environ.get("KZW_REMOTE_STATE_HOME", ""),
                            "published_hash": snapshot_hash(remote["snapshot"], panes)})
