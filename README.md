# kzw

<img src="assets/ai-slop-stamp.png" alt="AI SLOP warning" width="180">

https://github.com/user-attachments/assets/9a65a9ae-26fe-43d1-8700-7f1a89547301

## Installation

Requires Python 3.10+, Kitty 0.51+, and zmx. Remote hosts need SSH access, Python 3, and zmx.

```sh
git clone https://github.com/patricorgi/kitty-zmx-workspaces.git
cd kitty-zmx-workspaces
python3 install.py                 # installs only ~/.local/bin/kzw
export PATH="$HOME/.local/bin:$PATH"
```

For a custom directory, use `python3 install.py --bin-dir "$HOME/bin"` and add it to `PATH`.
The installed `kzw` runs without the checkout.

Optional: run `python3 install.py --kitty-hook-only` for native tabs
and confirmed pane closes. Use the same `--bin-dir`, reload Kitty’s config, and reattach.

## Basic commands

```sh
kzw create Demo --attach    # Create and open a workspace
kzw list                   # List workspaces
kzw detach Demo            # Close the Kitty view and keep sessions running
kzw attach Demo            # Reopen the workspace
kzw sessions list Demo     # Select a pane from an interactive terminal
kzw rename Demo NewName    # Rename a detached workspace
kzw kill-workspace Demo --host HOST  # Terminate a detached remote workspace and its sessions
kzw --help                 # Show all commands
```

Add `--host HOST` for a remote workspace. For example, run
`kzw create Demo --attach --host HOST`. Remote `list`, `inspect`, `inventory`,
`check`, and the session picker's inventory checks can reuse an existing SSH
connection to that host; no configuration is required.

`kill-workspace` prompts for confirmation. Detach an attached workspace first
with `kzw detach NAME --host HOST`. The shorter `kzw kill` spelling also works.

The workspace tab bar stays visible with just one tab, unless your Kitty
tab bar style is explicitly `hidden`. Ordinary Kitty windows keep their usual
tab count threshold.

To reorder managed tabs within a workspace, drag a tab along the tab bar
and drop it at the insertion marker, or use Kitty's `move_tab_backward` and
`move_tab_forward` actions. `kzw detach` saves the new order, including the
splits and focused pane, for the next attach. The separate **Local** companion
tab still restores last.

To download a file from a managed remote pane, run `ls --hyperlink=auto`
on the remote host (GNU `ls`), **Ctrl+Shift-click** the filename, then choose
**Save** and a local destination. kzw supplies the pane's SSH alias to Kitty's
remote-file menu, including when the alias uses a jump host.
With the personal Linux Yazi keymap, highlight or mark one file and press
**M**, then **d** to open that menu directly in the active managed remote pane.
The default Yazi opener (**Enter** or **o**) opens PDFs in Skim and images
in Preview on the Mac. For a live PDF preview you can also press **M**, then **p**. Skim
opens a local copy that updates after remote rebuilds. Enable automatic file
reload in Skim. Repeat **M p** on the same PDF to stop; detaching the pane
also stops the watch. The preview never writes back to the remote file.

When you run `kzw attach Demo --host HOST` from shared Kitty, a **Connecting**
window opens before SSH validation finishes. The saved layout loads into that
same window once verified. Close the preview to cancel, or check the window
and supervisor log if attachment fails.

To customize the centered splash, create `~/.config/kzw/splash.txt` (or
`$XDG_CONFIG_HOME/kzw/splash.txt`). It is UTF-8 plain text and can contain
multiple lines, blank lines, and the placeholders `{workspace}` and `{host}`:

```text
Preparing {workspace}

Connecting to {host}…
```

Missing, empty, invalid, or oversized files fall back to the default message.
No Kitty configuration or helper is needed.

## Limitations

- An SSH disconnect leaves the pane open and kzw retries the existing session.
  Only a verified shell exit closes the pane and removes it from the saved layout.

- kzw is experimental. Crashes or interrupted operations may require manual
  recovery, and recent layout changes may be lost.
- A workspace uses one origin host. To attach in the current Kitty instance,
  Kitty must run in single-instance mode with remote control enabled and a
  listening socket. Use `--new-kitty-instance` to open a separate instance.
- Native `new_tab` in a managed workspace requires Kitty 0.51+ and kzw's
  launch wrapper. Explicit commands and tabs opened from ordinary terminals
  are not adopted. In a warm benchmark on an M1 Pro, the wrapper added less
  than 1 ms to the median paired launch time for ordinary tabs and windows.
- In shared Kitty, a native pane close approved in Kitty's confirmation dialog
  removes that pane from the layout and terminates its zmx session. Use Kitty's
  `close_window_with_confirmation` action with any key you choose. Plain
  `close_window`, OS-window close, quit, and SSH loss do not authorize cleanup.
  A confirmed final pane close retires the workspace after cleanup, unless it
  still owns a separate local companion terminal.
  Use `kzw close-tab` when you want a single managed tab-close operation.
- Detach saves the layout. `kzw save` currently fails.
- Explicit right and below splits are currently reversed.
- Rename and kill require a detached workspace.
