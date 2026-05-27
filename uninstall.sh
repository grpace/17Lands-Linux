#!/usr/bin/env bash
# curl -fsSL https://github.com/grpace/17Lands-Linux/raw/main/uninstall.sh | bash
set -euo pipefail

say() { printf '\n==> %s\n' "$*"; }

stop_app() {
  local cmd
  for cmd in "${HOME}/.local/bin/seventeenlands-tray" /usr/bin/seventeenlands-tray; do
    [[ -x "${cmd}" ]] || continue
    "${cmd}" --quit 2>/dev/null || true
  done
  sleep 1
  pkill -f 'seventeenlands_tray.py|/seventeenlands$' 2>/dev/null || true
}

if ! rpm -q seventeenlands-tray >/dev/null 2>&1 \
   && [[ ! -x "${HOME}/.local/bin/seventeenlands-tray" ]]; then
  say "Nothing to uninstall."
  exit 0
fi

say "Removing 17Lands"
stop_app
rpm -q seventeenlands-tray >/dev/null 2>&1 && sudo dnf remove -y seventeenlands-tray
rm -f "${HOME}/.local/bin/seventeenlands-tray"
rm -f "${HOME}/.config/autostart/seventeenlands-tray.desktop"
rm -f "${HOME}/.local/share/applications/seventeenlands-tray.desktop"
rm -rf "${HOME}/.local/share/seventeenlands-tray"
rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/seventeenlands-tray.svg"
rm -f "${HOME}/.local/share/icons/hicolor/48x48/apps/seventeenlands-tray.png"
rm -f "${HOME}/.local/share/icons/hicolor/256x256/apps/seventeenlands-tray.png"
update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true

echo
echo "Done. Your token/config were kept."
