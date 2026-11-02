<div align="center">

<img src="assets/seventeenlands-tray.svg" width="88" alt="17Lands for Linux icon">

# 17Lands for Linux

**A KDE system tray app for the [17Lands](https://www.17lands.com/) MTG Arena log client.**

Installs in one command, starts at login, and keeps your drafts and matches syncing in the background.

[![CI](https://github.com/grpace/17Lands-Linux/actions/workflows/ci.yml/badge.svg)](https://github.com/grpace/17Lands-Linux/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

> [!NOTE]
> Unofficial and not affiliated with 17lands.com. This is a tray wrapper around the upstream [`seventeenlands`](https://pypi.org/project/seventeenlands/) client.

## Install

```bash
curl -fsSL https://github.com/grpace/17Lands-Linux/raw/main/install.sh | bash
```

A setup wizard asks for your token from [17lands.com/account](https://www.17lands.com/account), and that's it.

<details>
<summary>What the installer does</summary>

- Installs PyQt6 from your distro (dnf, apt, pacman or zypper) so windows match your theme
- Installs the `seventeenlands` client with `pip --user`, or `pipx` where pip installs are blocked
- Copies the app to `~/.local` and adds a systemd user service that starts it after login
- Disables any older `seventeenlands.service` you set up by hand

Requires Python 3.10+ and a desktop with a system tray. Built for KDE Plasma.

</details>

## Features

- **Runs in the background:** uploads happen as you play, with no need to open the window
- **Status at a glance:** the tray icon shows whether the client is syncing, watching or stopped
- **Dashboard:** last upload, session uploads, and a color-coded feed of drafts, wins, losses and uploads
- **One-click updates:** checks GitHub daily and shows an **Update** button when a new version is out
- **Fits your desktop:** follows your light or dark KDE theme
- **Self-healing:** restarts the client if it crashes, backing off up to 5 minutes
- **Auto-detects Arena:** finds `Player.log` for Steam (including Flatpak), Lutris and Wine
- **Private by default:** your token is never put on the command line, and its file is readable only by you

## Usage

| Action | Tray | Command |
|--------|------|---------|
| Open dashboard | Left-click | `seventeenlands-tray --show` |
| Start, stop, restart | Right-click | — |
| Settings | Middle-click | `seventeenlands-tray --settings` |
| Setup wizard | Right-click | `seventeenlands-tray --setup` |
| Update | **Update** button (shown when a new version is out) | `seventeenlands-tray --update` |
| Quit | Right-click | `seventeenlands-tray --quit` |

Closing the dashboard window keeps 17Lands running in your tray, so uploads continue. **Quit 17Lands** closes it completely and stops uploads until you open it again or log in next time.

**Tip:** To hide the icon until something needs attention, go to **System Settings → System Tray → 17Lands → Show Only in Popup**.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Burst of activity right after login | Normal: the client re-reads your Arena log on startup and shows **Catching Up** until it's done |
| No tray icon after login | Check `journalctl --user -u seventeenlands-tray` |
| "Client install looks broken" | Re-run the install command, or `seventeenlands-tray --update` (common after a Python upgrade) |
| Update didn't finish | See `~/.local/state/seventeenlands-tray/update.log` |
| "Detailed logs are disabled" | In Arena, enable **Options → Account → Detailed Logs (Plugin Support)** |
| Nothing is uploading | Check **Recent Activity** in the dashboard, or `~/.seventeenlands/seventeenlands.log` |

## Uninstall

```bash
curl -fsSL https://github.com/grpace/17Lands-Linux/raw/main/uninstall.sh | bash
```

Your token, settings, logs and the `seventeenlands` package are kept.

## Development

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests   # tests
ruff check . && shellcheck install.sh uninstall.sh                 # lint
./install.sh                                                       # install from your checkout
```

<br>

<div align="center">
  <sub><a href="LICENSE">MIT License</a> · No warranty · Built by <a href="https://greg.tech">Greg.Tech</a></sub>
</div>
