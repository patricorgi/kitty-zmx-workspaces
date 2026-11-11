# kzw

**Warning: This project is entirely AI slop.**

Persistent Kitty workspaces backed by zmx. **Experimental:** do not rely on kzw
for important work.

## Install

Requires Python 3.10+, Kitty 0.49+, and zmx. Remote workspaces also need SSH,
Python 3, and zmx on the remote host.

From this checkout:

```sh
python3 install.py
export PATH="$HOME/.local/bin:$PATH"
```

The installer refuses to overwrite existing commands. The launchers use this
checkout, so keep it in place and keep `~/.local/bin` on your `PATH`.

## Commands

```sh
kzw create Demo --attach       # Create and open a workspace
kzw list                       # List workspaces
kzw detach Demo                # Save and close Kitty; keep sessions running
kzw attach Demo                # Reopen a workspace
kzw rename Demo NewName        # Rename a detached workspace
kzw inspect Demo --json        # Show saved layout and revision
kzw inventory --json           # Join all zmx sessions to layout metadata
kzw events Demo --json         # Follow workspace events
kzw kill Demo                  # Terminate a detached workspace and its sessions
```

On a workspace's origin host, `kzw sessions list Demo` lets you select a split
and attach from any interactive terminal, without Kitty. Install kzw on that
host first. The host selector supports Python 3.8+. Use arrow keys or `j`/`k`
and Enter, or press `0`–`9` for a numbered pane; press `q` or Escape to cancel.
On the origin host, `kzw rename Demo NewName` targets that host's coordinator.

To show the last command for a Bash pane, opt in **inside that pane**:

```sh
eval "$(kzw sessions bash-hook)"
```

The hook stores the latest command in a private local file; commands may contain
secrets. Existing Bash sessions show `bash: unavailable` until enabled. The hook
does not edit shell configuration and refuses an existing `DEBUG` trap. Command
files remain on disk after sessions exit.

The hook also reports Bash prompts, command results and working directories to
Kitty, unless Kitty's Bash integration is already active. Managed splits and
tabs can inherit a validated reported directory; without one they use the
default. Command status is advisory and may be unavailable.

For a remote workspace, add `--host HOST` to each command:

```sh
kzw create Demo --host HOST --attach
```

Use `kzw attach Demo --inherit-appearance` to copy the calling Kitty window's
font size and available colors. Kitty remote control must be enabled for the
calling window. Active function markers are enabled in workspace panes; other
active marker types are rejected. Marker colors reflect configured values, not
per-window overrides. Marker initialization is asynchronous: **verify masking
before sharing**. It is visual only, not protection for copied or logged text.
The flag also works with `start`, `new-pane`, and `create --attach`.

By default, attach opens a new OS window in the calling Kitty instance and
starts a background supervisor. This requires Kitty started with
`--single-instance`, a `listen_on` address, and remote control enabled. Use
`--new-kitty-instance` for a separate instance, or `--hold` to keep supervision
in the foreground. The background log is in the workspace state directory.
Shared instances use their existing shortcuts and watcher configuration; the
shortcuts below are installed automatically only in separate instances. The tab
style is temporarily changed instance-wide. `kill` asks for confirmation and
requires a detached coordinator workspace.
Missing sessions are retained rather than automatically removed. An uncertain
restore can require manual investigation.

## Sources of truth

- **zmx:** session existence and terminal processes.
- **kzw server:** ownership and layouts keyed by session ID.
- **Kitty:** displayed layout; validated updates to the server.

Inventory includes unassigned sessions and pending operations. Failed queries
report unknown status; observations never silently remove saved layout entries.
See [the authority contract](README.md#sources-of-truth) for synchronization details.

## Kitty shortcuts

| Action | Shortcut |
| --- | --- |
| Detach | Command+Shift+W |
| Automatic split | Command+Enter |
| Split right / below | `kitty_mod+enter` / `kitty_mod+alt+enter` |
| New tab | Command+T or `kitty_mod+t` |
| Switch tab | Command+1–9; Command+0 for 10 |
| Close pane | Command+W or `kitty_mod+w` |
| Close tab | `kitty_mod+q` |
| Previous / next prompt | `kitty_mod+up` / `kitty_mod+down` |

Managed closes ask for confirmation and terminate the selected sessions;
unmanaged pane and tab closes are unsupported. Automatic splitting uses Kitty's
`splits` layout; other layouts determine their own placement. Tab titles list
split processes separated by `·`; `?` means a process is unknown. Tab numbers appear
after the title; Kitty may truncate them when space is limited.

## Limitations

- Crash recovery is not guaranteed.
- Unmanaged pane and tab closes are unsupported.
- Interrupted operations may need manual recovery.
- Remote coordination remains experimental.
- Conflicting leases require manual recovery.
- A workspace supports only one origin host.
- Kill requires a detached coordinator workspace.
- Renaming requires a detached workspace.
- Legacy workspaces need manual migration.
- Snapshots can miss recent changes after a crash.
- Restart old servers after code changes.
- Archived caches and event logs are not pruned.
- kzw is not yet a compiled Rust application.
- zmx may briefly recreate a vanished session.
- Bash command tracking needs an opt-in hook.
- Shell metadata may be missing or stale.
- Remote cwd inheritance is not yet verified.
- Bash command files are not pruned automatically.
- Per-window marker color changes are not copied.
- Marker masking can start after a pane appears.
- Missing sessions require manual investigation.
- Shared Kitty needs one remote-control instance.
- Shared Kitty uses its configured shortcuts.
- Shared tab styling affects the whole instance.
- SSH channel limits can force extra connections.
- Auto-close needs zmx 0.5.0 exit evidence.

Run `kzw --help` for more commands.

See [TESTING.md](README.md#limitations) for test commands and feature coverage.
