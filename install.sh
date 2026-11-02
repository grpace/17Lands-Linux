#!/usr/bin/env bash
# curl -fsSL https://github.com/grpace/17Lands-Linux/raw/main/install.sh | bash
set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-grpace/17Lands-Linux}"
GITHUB_REF="${GITHUB_REF:-main}"
SERVICE="seventeenlands-tray.service"
TRAY_BIN="${HOME}/.local/bin/seventeenlands-tray"
ICON_DIR="${HOME}/.local/share/icons/hicolor"
APPS_DIR="${HOME}/.local/share/applications"
UNIT_DIR="${HOME}/.config/systemd/user"
CLIENT_CONFIG="${HOME}/.mtga_follower.ini"
ROOT=""
DOWNLOAD_DIR=""

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

cleanup() {
  if [[ -n "${DOWNLOAD_DIR}" && -d "${DOWNLOAD_DIR}" ]]; then
    rm -rf "${DOWNLOAD_DIR}"
  fi
}
trap cleanup EXIT

[[ "${EUID}" -ne 0 ]] || die "Run this as your normal user, not root (it installs to ~/.local)."

# Running from a checkout? Otherwise (curl | bash) download the source.
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

fetch_source() {
  [[ -n "${ROOT}" && -f "${ROOT}/seventeenlands_tray.py" ]] && return 0
  have curl || die "curl is required."
  say "Downloading ${GITHUB_REPO}@${GITHUB_REF}"
  DOWNLOAD_DIR="$(mktemp -d)"
  curl -fsSL "https://codeload.github.com/${GITHUB_REPO}/tar.gz/${GITHUB_REF}" \
    | tar -xz -C "${DOWNLOAD_DIR}" --strip-components=1
  ROOT="${DOWNLOAD_DIR}"
  [[ -f "${ROOT}/seventeenlands_tray.py" ]] || die "Download failed."
}

stop_app() {
  systemctl --user stop "${SERVICE}" 2>/dev/null || true
  if [[ -x "${TRAY_BIN}" ]]; then
    "${TRAY_BIN}" --quit >/dev/null 2>&1 || true
  fi
}

remove_legacy_rpm() {
  if have rpm && rpm -q seventeenlands-tray >/dev/null 2>&1; then
    say "Removing old seventeenlands-tray RPM"
    sudo dnf remove -y seventeenlands-tray
  fi
}

# Install distro packages: pkg_install <dnf> <apt> <pacman> <zypper>
pkg_install() {
  if have dnf; then
    sudo dnf install -y "$1"
  elif have apt-get; then
    sudo apt-get update && sudo apt-get install -y "$2"
  elif have pacman; then
    sudo pacman -S --needed --noconfirm "$3"
  elif have zypper; then
    sudo zypper --non-interactive install "$4"
  else
    return 1
  fi
}

pyqt_ok() {
  python3 -c "from PyQt6.QtWidgets import QSystemTrayIcon; from PyQt6.QtNetwork import QLocalServer" \
    >/dev/null 2>&1
}

ensure_pyqt() {
  pyqt_ok && return 0
  # Prefer the distro build: it uses the desktop's Qt, so the windows follow the
  # KDE/Breeze theme. The pip wheel bundles its own Qt and looks out of place.
  say "Installing PyQt6 (system package; sudo may ask for your password)"
  pkg_install python3-pyqt6 python3-pyqt6 python-pyqt6 python3-PyQt6 || true
  pyqt_ok && return 0

  say "Falling back to PyQt6 from PyPI"
  python3 -m pip install --user PyQt6 || true
  pyqt_ok || die "PyQt6 is required. Install it with your package manager, then re-run."
}

ensure_pip() {
  python3 -m pip --version >/dev/null 2>&1 && return 0
  say "Installing pip"
  pkg_install python3-pip python3-pip python-pip python3-pip || return 1
}

install_client() {
  say "Installing the seventeenlands client"
  if have pipx && pipx list --short 2>/dev/null | grep -q '^seventeenlands '; then
    pipx upgrade seventeenlands
    return 0
  fi
  if ensure_pip && python3 -m pip install --user --upgrade seventeenlands; then
    return 0
  fi
  # Distros that mark the system Python as externally managed (PEP 668)
  # refuse pip --user installs; pipx is the supported route there.
  if ! have pipx; then
    say "Installing pipx"
    pkg_install pipx pipx python-pipx python3-pipx || true
  fi
  have pipx || die "Could not install seventeenlands. Install pipx, then re-run."
  pipx install seventeenlands
}

install_files() {
  say "Installing to ~/.local"
  mkdir -p "${HOME}/.local/bin" "${ICON_DIR}/scalable/apps" "${APPS_DIR}" "${UNIT_DIR}"
  rm -rf "${HOME}/.local/share/seventeenlands-tray"  # no longer used

  install -m 755 "${ROOT}/seventeenlands_tray.py" "${TRAY_BIN}"
  install -m 644 "${ROOT}/assets/seventeenlands-tray.svg" \
    "${ICON_DIR}/scalable/apps/seventeenlands-tray.svg"

  TRAY_SRC="${ROOT}" ICON_OUT="${ICON_DIR}" QT_QPA_PLATFORM=offscreen python3 - <<'PY' \
    || echo "Warning: could not render PNG icons (the SVG icon is still installed)." >&2
import os, sys
sys.path.insert(0, os.environ["TRAY_SRC"])
from PyQt6.QtWidgets import QApplication
import seventeenlands_tray as t
app = QApplication([])
for size in (48, 128, 256):
    out = f"{os.environ['ICON_OUT']}/{size}x{size}/apps"
    os.makedirs(out, exist_ok=True)
    t.make_badge_icon(size).pixmap(size, size).save(f"{out}/seventeenlands-tray.png")
PY

  cat > "${APPS_DIR}/seventeenlands-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=17Lands
GenericName=MTGA Log Client
Comment=Upload MTG Arena drafts and matches to 17Lands
Exec="${TRAY_BIN}" --show
Icon=seventeenlands-tray
Categories=Game;
Keywords=MTGA;Magic;Arena;17lands;draft;
StartupWMClass=17Lands
StartupNotify=false
X-KDE-StartupNotify=false
Actions=Settings;Quit;

[Desktop Action Settings]
Name=Settings
Exec="${TRAY_BIN}" --settings

[Desktop Action Quit]
Name=Quit
Exec="${TRAY_BIN}" --quit
EOF

  cat > "${UNIT_DIR}/${SERVICE}" <<EOF
[Unit]
Description=17Lands system tray
After=graphical-session.target
PartOf=graphical-session.target
StartLimitIntervalSec=600
StartLimitBurst=10

[Service]
Type=simple
# Give the Plasma system tray a moment to appear after login.
ExecStartPre=/bin/sleep 5
ExecStart="${TRAY_BIN}" --autostart --no-detach
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
EOF

  rm -f "${HOME}/.config/autostart/seventeenlands-tray.desktop"
  update-desktop-database "${APPS_DIR}" 2>/dev/null || true
  gtk-update-icon-cache -q "${ICON_DIR}" 2>/dev/null || true
}

secure_config() {
  # The client token lives here; older versions created it world-readable.
  if [[ -f "${CLIENT_CONFIG}" ]]; then
    chmod 600 "${CLIENT_CONFIG}"
  fi
}

disable_legacy_services() {
  local svc
  for svc in seventeenlands 17lands; do
    if systemctl --user is-enabled "${svc}.service" &>/dev/null; then
      say "Disabling old ${svc}.service (the tray manages the client now)"
      systemctl --user disable --now "${svc}.service" 2>/dev/null || true
    fi
  done
}

enable_autostart() {
  systemctl --user daemon-reload
  systemctl --user enable "${SERVICE}" >/dev/null
}

configured() {
  [[ -f "${CLIENT_CONFIG}" ]] \
    && grep -Eq '^token[[:space:]]*=[[:space:]]*[0-9a-fA-F]{32}[[:space:]]*$' "${CLIENT_CONFIG}"
}

session_has_display() {
  systemctl --user show-environment 2>/dev/null | grep -Eq '^(WAYLAND_DISPLAY|DISPLAY)='
}

start_app() {
  hash -r 2>/dev/null || true

  if ! configured; then
    if [[ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]]; then
      say "Setup wizard — get your token at 17lands.com/account"
      "${TRAY_BIN}" --setup-only || true
    fi
    if ! configured; then
      say "Setup not finished. From your desktop, run: seventeenlands-tray --setup"
      return 0
    fi
  fi

  if session_has_display; then
    systemctl --user restart --no-block "${SERVICE}"
    say "17Lands is starting in your system tray"
  else
    say "17Lands will start in your system tray at next login"
  fi
}

# --- main ---
fetch_source
have python3 || die "python3 is required."
stop_app
remove_legacy_rpm
ensure_pyqt
install_client
install_files
secure_config
disable_legacy_services
enable_autostart
start_app

echo
echo "Done. Commands: seventeenlands-tray --show | --settings | --quit"
