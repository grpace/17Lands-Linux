# 17Lands for Linux

Unofficial KDE system tray app for the [seventeenlands](https://pypi.org/project/seventeenlands/) MTGA log client.

> **Not affiliated with [17lands.com](https://www.17lands.com/).** This project only provides a tray wrapper around the upstream client.

---

## Install

```bash
curl -fsSL https://github.com/grpace/17Lands-Linux/raw/main/install.sh | bash
```

Run the same command again anytime to upgrade.

The installer sets up a **systemd user service** so 17Lands starts a few seconds after you log in — after the KDE tray is ready. No extra setup needed.

If you previously set up `seventeenlands.service` manually, the installer disables it so the tray app manages the client instead.

---

## First run

The setup wizard asks for your token from **[17lands.com/account](https://www.17lands.com/account)**.  
Arena log path is optional — leave blank to use the default.

---

## Uninstall

```bash
curl -fsSL https://github.com/grpace/17Lands-Linux/raw/main/uninstall.sh | bash
```

Your token and settings are kept.

---

## Daily use

| What | How |
|------|-----|
| Open dashboard | Click the tray icon |
| Open settings | `seventeenlands-tray --settings` |
| Quit | `seventeenlands-tray --quit` |

### Hide the tray icon on KDE

**System Settings → System Tray → 17Lands → Show only in popup**

The app keeps running in the background — open it from the tray popup or with `--settings`.

---

<p align="center">
  <sub>
    <a href="LICENSE">MIT License</a> · No warranty · <a href="https://greg.tech">Greg.Tech</a>
  </sub>
</p>
