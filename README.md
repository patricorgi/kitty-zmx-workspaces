# kzw

https://github.com/user-attachments/assets/03ad1bf8-9c07-40b8-91b7-ebbc5e3d2610

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

## Automated demo

Inside an existing single-instance Kitty on macOS,
`python3 scripts/demo.py --check` checks prerequisites and
`python3 scripts/demo.py --host HOST` runs the remote `Demo`: cmatrix (or a
built-in animation when unavailable), a
three-pane date/htop/Yazi tab, then detach/reattach. It uses a centered window
covering 90% of the usable display area and your existing privacy marker.
A full live Kitty/SSH demo passed,
including unchanged program identities after reattachment. Visual masking does
not guarantee that every secret is hidden. No recording is
started automatically. **Each normal run terminates and replaces the selected
demo workspace and its processes; `--check` does not.** After the final scene it
waits 10 additional seconds, then detaches and terminates the demo through the
normal CLI. This final cleanup has mocked coverage, not live verification.
Failures preserve remaining state for recovery. Other workspaces are left
alone, and old demo caches/journals are archived privately.

## Commands

```sh
kzw create Demo --attach       # Create and open a workspace
kzw list                       # List workspaces
kzw detach Demo                # Save and close Kitty; keep sessions running
kzw attach Demo                # Reopen a workspace
kzw rename Demo NewName        # Rename a detached workspace
kzw inspect Demo --json        # Show saved layout and revision
kzw inventory --json           # Join all zmx sessions to layout metadata
kzw check --host HOST          # Find stray sessions and ownership mismatches
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

Remote panes share a workspace-scoped SSH master when available. Setup is
serialized before attaching; stale/unavailable sharing falls back to a direct
connection without stopping other panes. Genuine SSH failures remain visible.

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
After its recorded Kitty process is positively gone, shared supervision now exits
without changing remote session ownership. Under a free supervisor lock and with
no pending local operations, `kill` may retire that proven-dead view's local
markers before normal confirmed termination. An unreachable live/unknown Kitty
still blocks cleanup; socket loss alone is not proof of detachment.
Missing sessions are retained rather than automatically removed. An uncertain
restore can require manual investigation.

## Sources of truth

- **zmx:** session existence and terminal processes.
- **kzw server:** ownership and layouts keyed by session ID.
- **Kitty:** displayed layout; validated updates to the server.

Inventory includes unassigned sessions and pending operations. Failed queries
report unknown status; observations never silently remove saved layout entries.

`kzw check [--host HOST] [--json]` matches every zmx session against the origin's
coordinator workspaces, including pending creation and cleanup references.
Strays are live sessions with no coordinator reference. It also reports missing
saved sessions, conflicting owners and invalid layouts. Exit status is 0 for a
clean check, 1 for findings or unavailable inventory. Legacy client-only layouts
are outside this check; it does not change ownership or terminate sessions.

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
| Managed Condor queue tab | Ctrl+Space, then Shift+Q |

Managed closes ask for confirmation and terminate the selected sessions;
unmanaged pane and tab closes are unsupported. Automatic splitting uses Kitty's
`splits` layout; other layouts determine their own placement. Tab titles list
split processes separated by `·`; `?` means a process is unknown. Tab numbers appear
before the title, with compact process separators such as `bash·nvim`.

With a compatible custom Kitty tab bar, coordinator workspaces show the workspace
name, host and CPU/RAM percentages at the right edge. Samples share a five-second
cache and the existing status connection. The dotfiles renderer uses chip/memory icons
colored green–yellow–red by usage; stale readings show `—`. The display hides when
space is tight. macOS RAM excludes free/inactive pages; Linux uses `MemAvailable`.
In the dotfiles renderer, an active privacy marker masks host and workspace names
in the status and matching tab-title text; the rest of the design stays visible.
New tabs and splits stay in the selected pane's workspace and inherit its zmx
shell's current directory on the origin host, without requiring shell integration.

The queue shortcut opens **HTCondor Queue** as a managed, zmx-backed tab in the
selected workspace. It appears in `kzw sessions list`, inventory and saved
layouts like other workspace terminals. The picker runs on the pane's origin
host, without tmux; the host needs `~/dotfiles/scripts/tmux_condor_queue.sh`,
`condor_q`, `fzf`, and `less`.
The launcher also finds an executable `~/.fzf/bin/fzf` when it is absent from
the non-interactive shell's PATH; an already-resolved `fzf` takes precedence.
Escape closes the picker; Enter follows Out, Ctrl+E follows Err, and Ctrl+R
refreshes. In the log viewer, Ctrl+C then `q` returns to the picker.
Picker startup errors stay visible until Enter; SSH failures use normal managed
pane reconnection. The selector shows the foreground process, such as `fzf` or
`less`, rather than necessarily the Kitty tab title.

Detach preserves the queue session; attach resumes that exact session without
rerunning its startup command. Escape ends the picker, and the existing
confirmed-exit path removes its pane. Managed close asks for confirmation like
any other workspace terminal. Interrupted creation retains its recovery journal.

Previously opened transient utility tabs are not converted or adopted. Close
the old picker and reopen it from a managed terminal to get a listed tab. Their
legacy filtering remains for compatibility while they are open. Shared Kitty
instances need the mapping in their own configuration (installed in this user's
dotfiles):

```conf
map --when-focus-on var:kzw_pane ctrl+space>shift+q kitten ~/workspaces/kitty-zmx-workspaces/kzw/utility_kitten.py
map --when-focus-on var:kzw_pane ctrl+space>ctrl+space send_key ctrl+space
```

The doubled prefix sends Ctrl+Space to the terminal. These conditional mappings
leave ordinary, non-kzw terminals' Ctrl+Space untouched. Live GUI/SSH verification
of the queue-tab feature remains outstanding.

## Limitations

- Right/below axes are currently reversed.
- `kzw save` fails; detach saves the layout.

- Crash recovery is not guaranteed.
- Unmanaged pane and tab closes are unsupported.
- Interrupted operations may need manual recovery.
- Remote coordination remains experimental.
- Conflicting leases require manual recovery.
- A workspace supports only one origin host.
- Kill requires a detached coordinator workspace.
- Renaming requires a detached workspace.
- Legacy workspaces need manual migration.
- Checks cover coordinator-owned workspaces.
- Snapshots can miss recent changes after a crash.
- Restart old servers after code changes.
- Exit-speed updates need a fresh attachment.
- Archived caches and event logs are not pruned.
- kzw is not yet a compiled Rust application.
- zmx may briefly recreate a vanished session.
- Bash command tracking needs an opt-in hook.
- Shell metadata may be missing or stale.
- Unavailable cwd falls back to the default.
- Nested SSH cwd is not the origin shell's cwd.
- Bash command files are not pruned automatically.
- Per-window marker color changes are not copied.
- Marker masking can start after a pane appears.
- Missing sessions require manual investigation.
- Shared Kitty needs one remote-control instance.
- Shared Kitty uses its configured shortcuts.
- Shared tab styling affects the whole instance.
- SSH channel limits can force extra connections.
- Auto-close needs zmx 0.5.0 exit evidence.
- Queue tabs need origin-host picker tools.
- Queue-tab GUI/SSH behavior remains unverified.
- Old queue utility tabs need manual replacement.

Run `kzw --help` for more commands.

Run the default tests with `python3 -m unittest discover -s tests -q`.
