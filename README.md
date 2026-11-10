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

Keep the supervisor terminal running while attached. `kill` asks for confirmation
and currently requires a detached coordinator workspace.
On macOS, when launched interactively from Kitty, kzw attempts to minimize the
calling Kitty OS window after the workspace is ready and restore it on detach.
This requires remote-control access and minimizes all tabs in that OS window;
the original shell and its processes stay running. If restoration fails, use the
Dock to reopen the window.
On coordinator attach, confirmed-missing sessions are removed from supported
saved split layouts; failed origin checks never authorize removal.

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
split processes separated by `│`; `?` means a process is unknown. Tab numbers appear
after the title; Kitty may truncate them when space is limited.

## Limitations

- Crash recovery is not guaranteed.
- Unmanaged pane and tab closes are unsupported.
- Some interrupted operations need manual recovery.
- Remote coordination remains experimental.
- A workspace supports only one origin host.
- Killing requires a detached coordinator workspace.
- Renaming requires a detached workspace.
- Legacy workspaces are not migrated automatically.
- Snapshots can miss recent changes after a crash.
- Old servers must be restarted after code changes.
- Archived caches and event logs are not pruned.
- kzw is not yet a compiled Rust application.
- zmx may briefly recreate a vanished session.
- Bash command tracking needs an opt-in hook.
- Shell metadata may be missing or stale.
- Remote cwd inheritance is not yet verified.
- Bash command files are not pruned automatically.
- Per-window marker color changes are not copied.
- Marker masking can start after a pane appears.
- Missing-pane pruning supports splits layouts only.

Run `kzw --help` for more commands.
