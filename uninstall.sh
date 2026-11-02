#!/usr/bin/env bash
# curl -fsSL https://github.com/grpace/17Lands-Linux/raw/main/uninstall.sh | bash
set -euo pipefail

SERVICE="seventeenlands-tray.service"
TRAY_BIN="${HOME}/.local/bin/seventeenlands-tray"
UNIT_FILE="${HOME}/.config/systemd/user/${SERVICE}"
ICON_DIR="${HOME}/.local/share/icons/hicolor"

say() { printf '\n==> %s\n' "$*"; }
has_rpm() { command -v rpm >/dev/null 2>&1 && rpm -q seventeenlands-tray >/dev/null 2>&1; }

if ! has_rpm && [[ ! -e "${TRAY_BIN}" && ! -f "${UNIT_FILE}" ]]; then
  say "Nothing to uninstall."
  exit 0
fi

say "Removing 17Lands tray"
systemctl --user disable --now "${SERVICE}" 2>/dev/null || true
if [[ -x "${TRAY_BIN}" ]]; then
  "${TRAY_BIN}" --quit >/dev/null 2>&1 || true
fi
if has_rpm; then
  sudo dnf remove -y seventeenlands-tray
fi

rm -f \
  "${TRAY_BIN}" \
  "${UNIT_FILE}" \
  "${HOME}/.config/autostart/seventeenlands-tray.desktop" \
  "${HOME}/.local/share/applications/seventeenlands-tray.desktop" \
  "${ICON_DIR}/scalable/apps/seventeenlands-tray.svg" \
  "${ICON_DIR}/48x48/apps/seventeenlands-tray.png" \
  "${ICON_DIR}/128x128/apps/seventeenlands-tray.png" \
  "${ICON_DIR}/256x256/apps/seventeenlands-tray.png"
rm -rf "${HOME}/.local/share/seventeenlands-tray"

update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache -q "${ICON_DIR}" 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true

echo
echo "Done. Kept: your token (~/.mtga_follower.ini), tray settings"
echo "(~/.config/seventeenlands-tray), client logs (~/.seventeenlands) and the"
echo "seventeenlands Python package. Remove the package with:"
echo "  python3 -m pip uninstall seventeenlands   (or: pipx uninstall seventeenlands)"
