# Nexus bridge

A local helper that lets AI tools on your computer, such as Claude Code, work
with the bookmarks and tabs in the **Nexus AI Browser** side panel. Every change
waits for your approval in the panel, and it can commit bookmark snapshots to a
Git repository you choose.

It runs only on your computer. Nothing is sent to the extension's developer.

## Install

Requirements: macOS or Linux, Python 3.9+, Git, and
[gitleaks](https://github.com/gitleaks/gitleaks) if you want Git sync.

1. In the side panel, open **Agent** and press **Connect agents**. The panel
   shows your extension id.
2. Run, with that id:

   ```sh
   git clone https://github.com/matgits/nexus-bridge
   ./nexus-bridge/install.sh --repo ~/path/to/bookmarks-repo <extension-id>
   ```

   To update later, or add another extension id: `git -C nexus-bridge pull`,
   then run `install.sh` again.

   `--repo` is optional: it is the Git repository your bookmarks sync to.
   Add `--no-push` to keep commits local.
3. Press **Check again** in the Agent tab.

The installer copies `nexusctl.py` to `~/Library/Application Support/NexusBridge`
(Linux: `~/.local/state/nexus-bridge`), registers it with each installed
Chromium-based browser for that extension id only, and links
`~/.local/bin/nexusctl`. `./nexus-bridge/install.sh --uninstall` removes it.

## Use

```sh
nexusctl status
nexusctl folders
nexusctl recent --limit 20
nexusctl add https://example.com --to "Bookmarks Bar/Reading"
nexusctl apply plan.json --dry-run
nexusctl sync
```

`nexusctl --help` lists every command. Changes show a card in the panel; you
approve, reject or later undo them. Tab access needs **Share open tabs** on.

## Safety

- The browser starts the helper only for the extension id you installed it with.
- Agents reach it through a socket readable only by your user account.
- Bookmark titles and URLs come from websites; agents must treat them as data.
- Git sync writes three snapshot files, scans them with gitleaks first and
  never force-pushes.

## Source

Published from the extension's source (`native/`); open issues here.
Licensed under the Apache License 2.0.
