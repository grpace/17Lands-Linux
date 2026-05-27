#!/usr/bin/env bash
# curl -fsSL https://github.com/grpace/17Lands-Linux/raw/main/install.sh | bash
set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-grpace/17Lands-Linux}"
ROOT=""
DOWNLOAD_DIR=""
TRAY_BIN="${HOME}/.local/bin/seventeenlands-tray"

# When piped from curl, skip yes/no prompts.
Piped=0
[[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" == "bash" ]] && Piped=1
[[ -t 0 ]] || Piped=1

say() { printf '\n==> %s\n' "$*"; }

cleanup() {
  [[ -n "${DOWNLOAD_DIR}" && -d "${DOWNLOAD_DIR}" ]] && rm -rf "${DOWNLOAD_DIR}"
}
trap cleanup EXIT

if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

fetch_source() {
  [[ -f "${ROOT}/seventeenlands_tray.py" ]] && return 0
  command -v curl >/dev/null || { echo "curl is required." >&2; exit 1; }
  say "Downloading from GitHub"
  DOWNLOAD_DIR="$(mktemp -d)"
  curl -fsSL "https://codeload.github.com/${GITHUB_REPO}/tar.gz/refs/heads/main" \
    | tar -xz -C "${DOWNLOAD_DIR}" --strip-components=1
  ROOT="${DOWNLOAD_DIR}"
  [[ -f "${ROOT}/seventeenlands_tray.py" ]] || { echo "Download failed." >&2; exit 1; }
}

stop_app() {
  local cmd
  for cmd in "${TRAY_BIN}" /usr/bin/seventeenlands-tray; do
    [[ -x "${cmd}" ]] || continue
    "${cmd}" --quit 2>/dev/null || true
  done
  sleep 1
  pkill -f 'seventeenlands_tray.py|/seventeenlands$' 2>/dev/null || true
}

ensure_deps() {
  command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }

  if ! python3 -m pip --version >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
      say "Installing python3-pip"
      sudo dnf install -y python3-pip
    else
      echo "python3-pip is required." >&2; exit 1
    fi
  fi

  if python3 -c "from PyQt6.QtWidgets import QSystemTrayIcon" 2>/dev/null; then
    return 0
  fi

  say "Installing PyQt6"
  python3 -m pip install --user PyQt6 2>/dev/null \
    || true

  if python3 -c "from PyQt6.QtWidgets import QSystemTrayIcon" 2>/dev/null; then
    return 0
  fi

  if [[ "${Piped}" -eq 0 ]]; then
    read -r -p "Install PyQt6 with your package manager? [Y/n] " a
    [[ "${a}" =~ ^[Nn]$ ]] && { echo "PyQt6 is required." >&2; exit 1; }
  fi

  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3-pyqt6
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y python3-pyqt6
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --needed --noconfirm python-pyqt6
  else
    echo "Install PyQt6, then re-run this script." >&2
    exit 1
  fi

  python3 -c "from PyQt6.QtWidgets import QSystemTrayIcon" 2>/dev/null \
    || { echo "PyQt6 is required." >&2; exit 1; }
}

install_files() {
  local share="${HOME}/.local/share/seventeenlands-tray"
  local icon="${HOME}/.local/share/icons/hicolor"
  local apps="${HOME}/.local/share/applications"
  local auto="${HOME}/.config/autostart"

  say "Installing to ~/.local"
  mkdir -p "${HOME}/.local/bin" "${share}" "${icon}/scalable/apps" \
    "${icon}/48x48/apps" "${icon}/256x256/apps" "${apps}" "${auto}"

  install -m 755 "${ROOT}/seventeenlands_tray.py" "${TRAY_BIN}"
  install -m 644 "${ROOT}/assets/seventeenlands-tray.svg" "${share}/seventeenlands-tray.svg"
  install -m 644 "${ROOT}/assets/seventeenlands-tray.svg" "${icon}/scalable/apps/seventeenlands-tray.svg"

  python3 - <<PY
import sys; sys.path.insert(0, "${ROOT}")
import os; os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication
import seventeenlands_tray as t
app = QApplication([])
i = t.make_17_icon(QColor("#f2f2f2"), 256)
i.pixmap(256, 256).save("${icon}/256x256/apps/seventeenlands-tray.png")
i.pixmap(48, 48).save("${icon}/48x48/apps/seventeenlands-tray.png")
PY

  for dest in "${apps}/seventeenlands-tray.desktop" "${auto}/seventeenlands-tray.desktop"; do
    cat > "${dest}" <<EOF
[Desktop Entry]
Type=Application
Name=17Lands
Comment=MTGA log client tray
Exec=${TRAY_BIN}
Icon=seventeenlands-tray
StartupWMClass=17Lands
StartupNotify=false
X-KDE-StartupNotify=false
EOF
  done
  printf 'X-KDE-autostart-phase=2\nHidden=false\n' >> "${auto}/seventeenlands-tray.desktop"

  update-desktop-database "${apps}" 2>/dev/null || true
  gtk-update-icon-cache "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true
}

configured() {
  [[ -f "${HOME}/.mtga_follower.ini" ]] && grep -q '^token = ' "${HOME}/.mtga_follower.ini" 2>/dev/null
}

start_app() {
  hash -r 2>/dev/null || true
  if ! configured; then
    say "Setup wizard — get your token at 17lands.com/account"
    "${TRAY_BIN}" --setup --no-detach
  else
    DISPLAY="${DISPLAY:-:0}" "${TRAY_BIN}" &
    say "Running in the background"
  fi
}

# --- main ---
fetch_source
stop_app
rpm -q seventeenlands-tray >/dev/null 2>&1 && sudo dnf remove -y seventeenlands-tray
ensure_deps
say "Installing seventeenlands"
python3 -m pip install --user --upgrade pip seventeenlands
install_files
start_app

echo
echo "Done. Commands: seventeenlands-tray --settings | --quit"
