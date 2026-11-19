# kzw

<img src="assets/ai-slop-stamp.png" alt="AI SLOP warning" width="180">

https://github.com/user-attachments/assets/c602f153-64fb-479f-bbda-e4553450ae67

## Installation

Install Python 3.10+, Kitty 0.49+, and zmx. For remote workspaces, make sure
you can SSH to the host and that it has Python 3 and zmx.

```sh
git clone https://github.com/patricorgi/kitty-zmx-workspaces.git
cd kitty-zmx-workspaces
python3 install.py
export PATH="$HOME/.local/bin:$PATH"
```

Keep the checkout in place. Add the PATH line to your shell configuration to
use the installed commands in new terminals.

## Basic commands

```sh
kzw create Demo --attach    # Create and open a workspace
kzw list                   # List workspaces
kzw detach Demo            # Close the Kitty view and keep sessions running
kzw attach Demo            # Reopen the workspace
kzw sessions list Demo     # Select a pane from an interactive terminal
kzw rename Demo NewName    # Rename a detached workspace
kzw kill Demo              # Terminate a detached workspace and its sessions
kzw --help                 # Show all commands
```

Add `--host HOST` for a remote workspace. For example, run
`kzw create Demo --attach --host HOST`.

## Limitations

- kzw is experimental. Crashes or interrupted operations may require manual
  recovery, and recent layout changes may be lost.
- A workspace uses one origin host. To attach in the current Kitty instance,
  Kitty must run in single-instance mode with remote control enabled and a
  listening socket. Use `--new-kitty-instance` to open a separate instance.
- Detach saves the layout. `kzw save` currently fails.
- Explicit right and below splits are currently reversed.
- Rename and kill require a detached workspace. Unmanaged pane and tab closes
  are unsupported.
