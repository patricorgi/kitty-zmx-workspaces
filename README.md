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
kzw inspect Demo --json        # Show saved layout and revision
kzw events Demo --json         # Follow workspace events
kzw kill Demo                  # Terminate a detached workspace and its sessions
```

For a remote workspace, add `--host HOST` to each command:

```sh
kzw create Demo --host HOST --attach
```

Keep the supervisor terminal running while attached. `kill` asks for confirmation
and currently requires a detached coordinator workspace.

## Kitty shortcuts

| Action | Shortcut |
| --- | --- |
| Detach | Command+Shift+W |
| Split right / below | `kitty_mod+enter` / `kitty_mod+alt+enter` |
| New tab | `kitty_mod+t` |
| Close pane / tab | `kitty_mod+w` / `kitty_mod+q` |

Managed closes ask for confirmation and terminate the selected sessions;
unmanaged pane and tab closes are unsupported.

## Limitations

- Crash recovery is not guaranteed.
- Unmanaged pane and tab closes are unsupported.
- Some interrupted operations need manual recovery.
- Remote coordination remains experimental.
- A workspace supports only one origin host.
- Killing requires a detached coordinator workspace.
- Legacy workspaces are not migrated automatically.
- Snapshots can miss recent changes after a crash.
- Old servers must be restarted after code changes.
- Archived caches and event logs are not pruned.
- kzw is not yet a compiled Rust application.

Run `kzw --help` for more commands.
