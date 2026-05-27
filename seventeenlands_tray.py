#!/usr/bin/env python3
"""Lightweight system tray wrapper that keeps seventeenlands running."""

from __future__ import annotations

import configparser
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QProcess, QTimer, Qt
from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

APP_ID = "seventeenlands-tray"
APP_NAME = "17Lands"
DETACH_ENV = "SEVENTEENLANDS_TRAY_DETACHED"


def resolve_share_dir() -> Path:
    module_dir = Path(__file__).resolve().parent
    env_share = os.environ.get("SEVENTEENLANDS_TRAY_SHARE", "").strip()
    candidates = [
        Path(env_share) if env_share else None,
        Path("/usr/share/seventeenlands-tray"),
        module_dir / "assets",
        module_dir.parent / "share" / "seventeenlands-tray",
        Path.home() / ".local/share/seventeenlands-tray",
    ]
    for candidate in candidates:
        if candidate and (candidate / "seventeenlands-tray.svg").exists():
            return candidate
    return Path.home() / ".local/share/seventeenlands-tray"


SHARE_DIR = resolve_share_dir()
SEVENTEENLANDS_CMD = shutil.which("seventeenlands") or str(
    Path.home() / ".local" / "bin" / "seventeenlands"
)
LOG_FILE = Path.home() / ".seventeenlands" / "seventeenlands.log"
CONFIG_FILE = Path.home() / ".mtga_follower.ini"
TRAY_SETTINGS_DIR = Path.home() / ".config" / "seventeenlands-tray"
TRAY_SETTINGS_FILE = TRAY_SETTINGS_DIR / "settings.ini"
COMMAND_PORT = 47171
OPEN_SETTINGS_ENV = "SEVENTEENLANDS_TRAY_OPEN_SETTINGS"
SETUP_COMMAND = "setup"
RESTART_DELAY_MS = 5000
STATUS_POLL_MS = 2000
SYNC_ACTIVE_SECONDS = 12
IDLE_SECONDS = 300
LOG_TAIL_BYTES = 120_000
MAX_RECENT_EVENTS = 20
PASSIVE_TRAY_STATES = frozenset({"up_to_date", "watching", "idle"})
CREDIT_URL = "https://greg.tech"

LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{8} \d{6}\.\d+),(?P<level>\w+),(?P<logger>[^,]+),(?P<message>.+)$"
)
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$", re.I)

SYNC_KEYWORDS = (
    "Submitting queued game result",
    "Adding game history",
    "Completed game:",
    "Submitting mastery progress",
    "Collection submission",
    "Submitting inventory",
    "Human draft pick",
    "Draft pick:",
    "Deck submission",
)


class ClientSettings:
    token: str | None = None
    log_file: str | None = None

    @classmethod
    def load(cls) -> ClientSettings:
        settings = cls()
        config = configparser.ConfigParser()
        if CONFIG_FILE.exists():
            config.read(CONFIG_FILE)
            if "client" in config:
                token = config["client"].get("token", "").strip()
                if TOKEN_RE.match(token):
                    settings.token = token

        tray_config = configparser.ConfigParser()
        if TRAY_SETTINGS_FILE.exists():
            tray_config.read(TRAY_SETTINGS_FILE)
            if "client" in tray_config:
                log_file = tray_config["client"].get("log_file", "").strip()
                if log_file:
                    settings.log_file = log_file
        return settings

    def save_token(self, token: str) -> None:
        config = configparser.ConfigParser()
        if CONFIG_FILE.exists():
            config.read(CONFIG_FILE)
        if "client" not in config:
            config["client"] = {}
        config["client"]["token"] = token
        with CONFIG_FILE.open("w") as handle:
            config.write(handle)
        self.token = token

    def save_log_file(self, log_file: str | None) -> None:
        TRAY_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        config = configparser.ConfigParser()
        if TRAY_SETTINGS_FILE.exists():
            config.read(TRAY_SETTINGS_FILE)
        if "client" not in config:
            config["client"] = {}
        if log_file:
            config["client"]["log_file"] = log_file
            self.log_file = log_file
        elif "log_file" in config["client"]:
            del config["client"]["log_file"]
            self.log_file = None
        with TRAY_SETTINGS_FILE.open("w") as handle:
            config.write(handle)

    def masked_token(self) -> str:
        if not self.token:
            return "Not configured"
        return f"{self.token[:4]}...{self.token[-4:]}"

    @classmethod
    def is_configured(cls) -> bool:
        return cls.load().token is not None

    def configure(self, token: str, log_file: str | None) -> None:
        self.save_token(token)
        self.save_log_file(log_file)


def detect_arena_log_paths() -> list[Path]:
    username = os.environ.get("USER") or os.environ.get("LOGNAME") or "steamuser"
    candidates = [
        Path.home()
        / ".local/share/Steam/steamapps/compatdata/2141910/pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log",
        Path.home()
        / ".steam/steam/steamapps/compatdata/2141910/pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log",
        Path.home()
        / f"Games/magic-the-gathering-arena/drive_c/users/{username}/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log",
        Path.home()
        / f".wine/drive_c/users/{username}/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log",
    ]
    seen: set[Path] = set()
    found: list[Path] = []
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if path.exists() and resolved not in seen:
            seen.add(resolved)
            found.append(path)
    return found


def seventeenlands_installed() -> bool:
    if shutil.which("seventeenlands"):
        return True
    return Path.home().joinpath(".local/bin/seventeenlands").exists()


@dataclass
class ParsedEvent:
    timestamp: datetime
    level: str
    summary: str


@dataclass
class ClientSnapshot:
    state: str
    state_detail: str
    account: str | None = None
    arena_log: str | None = None
    last_event_at: datetime | None = None
    last_sync_at: datetime | None = None
    session_uploads: int = 0
    recent_events: list[ParsedEvent] = field(default_factory=list)
    process_running: bool = False
    external_process: bool = False


class LogAnalyzer:
    @staticmethod
    def read_tail(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> list[str]:
        if not path.exists():
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                data = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        return [line.strip() for line in data.splitlines() if line.strip()]

    @staticmethod
    def parse_timestamp(raw: str) -> datetime | None:
        try:
            return datetime.strptime(raw, "%Y%m%d %H%M%S.%f")
        except ValueError:
            return None

    @classmethod
    def summarize_message(cls, message: str) -> str | None:
        if message.startswith("Following along "):
            return "Watching Arena log for new events"

        if message.startswith("Updating user info:"):
            screen_name = cls._extract_screen_name(message)
            return f"Logged in as {screen_name}" if screen_name else "Logged in to Arena"

        if message.startswith("Submitting queued game result"):
            return "Uploading match to 17Lands"

        if message.startswith("Added pending match result"):
            won = "'won_match': True" in message
            return "Match win queued for upload" if won else "Match loss queued for upload"

        if message.startswith("Completed game:"):
            event = cls._extract_event_name(message)
            return f"Game completed ({event})" if event else "Game completed"

        if "Human draft pick" in message:
            pack, pick = cls._extract_draft_numbers(message)
            if pack and pick:
                return f"Draft pick recorded (pack {pack}, pick {pick})"
            return "Draft pick recorded"

        if message.startswith("Joined draft pod:"):
            event = message.split("Joined draft pod:", 1)[1].strip()
            return f"Joined draft: {event}"

        if message.startswith("Joined event successfully"):
            return "Joined Arena event"

        if message.startswith("Deck submission"):
            event = cls._extract_event_name(message)
            return f"Deck submitted ({event})" if event else "Deck submitted"

        if message.startswith("Updated ongoing events"):
            return "Checked ongoing Arena events"

        if message.startswith("Submitting mastery progress"):
            return "Uploading mastery progress"

        if message.startswith("Parsed rank info"):
            return "Rank info updated"

        if message.startswith("Got minimum client version"):
            return "Verified client version"

        if message.startswith("Parsing the previous log"):
            return "Catching up on previous Arena log"

        if "Detailed logs are disabled" in message:
            return "Warning: detailed Arena logs are disabled"

        if message.startswith("Found no files to parse"):
            return "Could not find Arena Player.log"

        if message.startswith("Exiting"):
            return "Client stopped"

        if "Adding game history" in message:
            return "Packaging game history for upload"

        return None

    @staticmethod
    def _extract_screen_name(message: str) -> str | None:
        match = re.search(r"'screen_name': '([^']+)'", message)
        return match.group(1) if match else None

    @staticmethod
    def _extract_event_name(message: str) -> str | None:
        match = re.search(r"'event_name': '([^']+)'", message)
        return match.group(1) if match else None

    @staticmethod
    def _extract_draft_numbers(message: str) -> tuple[str | None, str | None]:
        pack = re.search(r"'pack_number': (\d+)", message)
        pick = re.search(r"'pick_number': (\d+)", message)
        return (
            pack.group(1) if pack else None,
            pick.group(1) if pick else None,
        )

    @classmethod
    def build_snapshot(
        cls,
        *,
        process_running: bool,
        external_process: bool,
        starting: bool,
    ) -> ClientSnapshot:
        lines = cls.read_tail(LOG_FILE)
        now = datetime.now()
        recent_events: list[ParsedEvent] = []
        account: str | None = None
        arena_log: str | None = None
        last_event_at: datetime | None = None
        last_sync_at: datetime | None = None
        session_uploads = 0
        has_error = False
        missing_log = False
        saw_following = False
        recent_sync_activity = False

        for line in lines:
            match = LOG_LINE_RE.match(line)
            if not match:
                continue

            ts = cls.parse_timestamp(match.group("ts"))
            if ts is None:
                continue

            level = match.group("level")
            message = match.group("message")
            last_event_at = ts

            if level == "ERROR":
                has_error = True

            if message.startswith("Following along "):
                arena_log = message.split("Following along ", 1)[1].strip()
                saw_following = True

            screen_name = cls._extract_screen_name(message)
            if screen_name:
                account = screen_name

            if message.startswith("Submitting queued game result"):
                last_sync_at = ts
                session_uploads += 1

            if any(keyword in message for keyword in SYNC_KEYWORDS):
                if ts >= now - timedelta(seconds=SYNC_ACTIVE_SECONDS):
                    recent_sync_activity = True

            summary = cls.summarize_message(message)
            if summary:
                recent_events.append(ParsedEvent(timestamp=ts, level=level, summary=summary))

            if message.startswith("Found no files to parse"):
                missing_log = True

        recent_events = recent_events[-MAX_RECENT_EVENTS:]
        recent_events.reverse()

        if not process_running:
            state = "stopped"
            state_detail = "Client is not running"
        elif starting:
            state = "starting"
            state_detail = "Starting seventeenlands..."
        elif has_error:
            state = "error"
            state_detail = "Recent errors in client log"
        elif missing_log and not saw_following:
            state = "waiting"
            state_detail = "Waiting for Arena Player.log"
        elif recent_sync_activity:
            state = "syncing"
            state_detail = "Uploading or processing Arena data"
        elif last_event_at and now - last_event_at <= timedelta(seconds=IDLE_SECONDS):
            if last_sync_at and now - last_sync_at <= timedelta(minutes=30):
                state = "up_to_date"
                state_detail = "Up to date with latest logs"
            else:
                state = "watching"
                state_detail = "Watching Arena log"
        else:
            state = "idle"
            state_detail = "Idle — no recent Arena activity"

        if external_process and process_running:
            state_detail += " (external process)"

        return ClientSnapshot(
            state=state,
            state_detail=state_detail,
            account=account,
            arena_log=arena_log,
            last_event_at=last_event_at,
            last_sync_at=last_sync_at,
            session_uploads=session_uploads,
            recent_events=recent_events,
            process_running=process_running,
            external_process=external_process,
        )


class SingleInstance:
    """Prevent multiple tray instances and accept remote commands."""

    def __init__(self, app_id: str, on_command: callable | None = None) -> None:
        self._on_command = on_command
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._socket.bind(("127.0.0.1", COMMAND_PORT))
            self._socket.listen(5)
            self._socket.setblocking(False)
        except OSError:
            self._socket.close()
            raise RuntimeError("17Lands tray is already running")

        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll_commands)
        self._poll_timer.start(400)

    def _poll_commands(self) -> None:
        while True:
            try:
                connection, _address = self._socket.accept()
            except BlockingIOError:
                break
            try:
                data = connection.recv(256).decode("utf-8", errors="replace").strip()
            finally:
                connection.close()
            if data and self._on_command:
                self._on_command(data)

    def close(self) -> None:
        self._poll_timer.stop()
        self._socket.close()


def send_instance_command(command: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", COMMAND_PORT), timeout=1.0) as sock:
            sock.sendall(f"{command}\n".encode("utf-8"))
        return True
    except OSError:
        return False


def load_app_icon() -> QIcon:
    for path in (
        SHARE_DIR / "seventeenlands-tray.svg",
        Path("/usr/share/icons/hicolor/256x256/apps/seventeenlands-tray.png"),
        Path("/usr/share/icons/hicolor/48x48/apps/seventeenlands-tray.png"),
        Path.home() / ".local/share/icons/hicolor/256x256/apps/seventeenlands-tray.png",
        Path.home() / ".local/share/icons/hicolor/48x48/apps/seventeenlands-tray.png",
        Path(__file__).resolve().parent / "assets" / "seventeenlands-tray.svg",
    ):
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return make_17_icon(QColor("#f2f2f2"))


class TrayDBusStatus:
    """Set KDE 'Show when relevant' via StatusNotifierItem Status property."""

    KDE_SNI_IFACE = "org.kde.StatusNotifierItem"
    FREEDESKTOP_SNI_IFACE = "org.freedesktop.StatusNotifierItem"
    SNI_PATH = "/StatusNotifierItem"
    PROPS_IFACE = "org.freedesktop.DBus.Properties"
    DBUS_SERVICE = "org.freedesktop.DBus"
    DBUS_PATH = "/org/freedesktop/DBus"
    DBUS_IFACE = "org.freedesktop.DBus"
    WATCHER_SERVICE = "org.kde.StatusNotifierWatcher"
    WATCHER_PATH = "/StatusNotifierWatcher"
    WATCHER_IFACE = "org.kde.StatusNotifierWatcher"

    def __init__(self) -> None:
        self._last_status: str | None = None
        self._service_name: str | None = None
        self._dbus_available = False

    def _item_interface(self, service: str) -> QDBusInterface:
        return QDBusInterface(
            service,
            self.SNI_PATH,
            self.KDE_SNI_IFACE,
            QDBusConnection.sessionBus(),
        )

    def _service_pid(self, service: str) -> int | None:
        bus = QDBusConnection.sessionBus()
        iface = QDBusInterface(
            self.DBUS_SERVICE,
            self.DBUS_PATH,
            self.DBUS_IFACE,
            bus,
        )
        reply = iface.call("GetConnectionUnixProcessID", service)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            return None
        args = reply.arguments()
        if not args:
            return None
        return int(args[0])

    def _matches_app(self, service: str) -> bool:
        item = self._item_interface(service)
        item_id = str(item.property("Id") or "")
        title = str(item.property("Title") or "")
        return item_id in {APP_NAME, APP_ID} or title == APP_NAME

    def _resolve_service(self, *, force_refresh: bool = False) -> str | None:
        if self._service_name and not force_refresh:
            return self._service_name

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return None

        our_pid = os.getpid()
        matches: list[str] = []

        base = bus.baseService()
        if base and self._matches_app(base) and self._service_pid(base) == our_pid:
            matches.append(base)

        watcher = QDBusInterface(
            self.WATCHER_SERVICE,
            self.WATCHER_PATH,
            self.WATCHER_IFACE,
            bus,
        )
        registered = watcher.property("RegisteredStatusNotifierItems")
        if registered:
            for entry in registered:
                if "/" not in entry:
                    continue
                service, path_part = entry.split("/", 1)
                path = f"/{path_part}"
                if path != self.SNI_PATH:
                    continue
                if self._matches_app(service) and self._service_pid(service) == our_pid:
                    matches.append(service)

        if not matches:
            return None

        service = matches[-1]
        self._service_name = service
        return service

    def _try_set_status(self, service: str, dbus_status: str) -> bool:
        bus = QDBusConnection.sessionBus()
        props = QDBusInterface(service, self.SNI_PATH, self.PROPS_IFACE, bus)
        item = self._item_interface(service)
        for iface in (self.KDE_SNI_IFACE, self.FREEDESKTOP_SNI_IFACE):
            reply = props.call("Set", iface, "Status", dbus_status)
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                continue
            actual = str(item.property("Status") or "")
            if actual == dbus_status:
                return True
        return False

    def set_for_state(self, state: str) -> bool:
        if state in PASSIVE_TRAY_STATES:
            dbus_status = "Passive"
        elif state == "error":
            dbus_status = "NeedsAttention"
        else:
            dbus_status = "Active"

        if dbus_status == self._last_status and self._dbus_available:
            return True

        service = self._resolve_service(force_refresh=not self._dbus_available)
        if not service:
            return False

        if not self._try_set_status(service, dbus_status):
            self._service_name = None
            self._dbus_available = False
            self._last_status = None
            return False

        self._last_status = dbus_status
        self._dbus_available = True
        return True

    def reset(self) -> None:
        self._service_name = None
        self._last_status = None
        self._dbus_available = False


def make_17_icon(color: QColor, size: int = 64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    font = QFont("DejaVu Sans")
    font.setBold(True)
    font.setPixelSize(int(size * 0.58))
    painter.setFont(font)
    painter.setPen(color)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "17")
    painter.end()
    return QIcon(pixmap)


def load_icon_variants() -> tuple[QIcon, QIcon, QIcon, QIcon]:
    return (
        make_17_icon(QColor("#f2f2f2")),
        make_17_icon(QColor("#5c5c5c")),
        make_17_icon(QColor("#9a9a9a")),
        make_17_icon(QColor("#6ec6ff")),
    )


def detach_from_terminal() -> None:
    """Re-launch detached so closing the terminal does not stop the tray app."""
    if os.environ.get(DETACH_ENV) == "1":
        return
    if "--no-detach" in sys.argv:
        sys.argv.remove("--no-detach")
        return
    if not sys.stdin.isatty():
        return

    env = os.environ.copy()
    env[DETACH_ENV] = "1"
    subprocess.Popen(
        [sys.executable, *sys.argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
        close_fds=True,
    )
    print("17Lands tray started in the background.")
    raise SystemExit(0)


def format_relative(when: datetime | None) -> str:
    if when is None:
        return "Never"
    delta = datetime.now() - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "Just now"
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} min ago"
    hours = seconds // 3600
    return f"{hours} hr ago"


def short_log_path(path: str | None) -> str:
    if not path:
        return "Not detected yet"
    name = Path(path).name
    parent = Path(path).parent.as_posix()
    if "steam" in parent.lower():
        return f"{name} (Steam)"
    if "lutris" in parent.lower() or "games/" in parent.lower():
        return f"{name} (Lutris)"
    if "wine" in parent.lower():
        return f"{name} (Wine)"
    return name


STATE_STYLES = {
    "up_to_date": ("#1f8f4a", "#e8f7ee"),
    "watching": ("#1f8f4a", "#e8f7ee"),
    "syncing": ("#1b6eb8", "#e7f2fc"),
    "starting": ("#9a6700", "#fff6df"),
    "idle": ("#6b7280", "#f3f4f6"),
    "waiting": ("#9a6700", "#fff6df"),
    "stopped": ("#b42318", "#fdecec"),
    "error": ("#b42318", "#fdecec"),
}


class SetupWizard(QWizard):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} Setup")
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(520, 420)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)

        self._settings = ClientSettings.load()
        self._token_input = QLineEdit(self._settings.token or "")
        self._token_input.setPlaceholderText("Paste your 32-character token here")
        self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._log_input = QLineEdit(self._settings.log_file or "")

        self.addPage(self._welcome_page())
        self.addPage(self._token_page())
        self.addPage(self._log_page())
        self.addPage(self._finish_page())

    def _welcome_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Welcome to 17Lands")
        layout = QVBoxLayout(page)

        client_ok = seventeenlands_installed()
        status = (
            "found"
            if client_ok
            else "not found — run install.sh or pip3 install --user seventeenlands"
        )
        intro = QLabel(
            "<p>This setup will connect 17Lands to your MTGA account so draft and "
            "match data sync automatically.</p>"
            f"<p><b>seventeenlands client:</b> {status}</p>"
            "<p>You will need a free account at "
            '<a href="https://www.17lands.com/">17lands.com</a>.</p>'
        )
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        layout.addWidget(intro)
        return page

    def _token_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Sign in to 17Lands")
        layout = QVBoxLayout(page)

        intro = QLabel(
            "Copy your client token from "
            '<a href="https://www.17lands.com/account">17lands.com/account</a> '
            "and paste it below."
        )
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        layout.addWidget(intro)
        layout.addWidget(self._token_input)

        open_account = QPushButton("Open 17lands.com/account in browser")
        open_account.clicked.connect(
            lambda: subprocess.Popen(
                ["xdg-open", "https://www.17lands.com/account"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        layout.addWidget(open_account)
        page.registerField("token*", self._token_input)
        return page

    def _log_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Arena log file")
        layout = QVBoxLayout(page)

        intro = QLabel(
            "17Lands reads MTGA's Player.log. Leave blank to auto-detect, "
            "or pick the file if you use Steam, Lutris, or Wine."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        detected = detect_arena_log_paths()
        if detected:
            layout.addWidget(QLabel("Detected log files:"))
            for path in detected:
                button = QPushButton(str(path))
                button.clicked.connect(
                    lambda _checked, p=path: self._log_input.setText(str(p))
                )
                layout.addWidget(button)
        else:
            layout.addWidget(
                QLabel("No Player.log found yet — you can configure this later in Settings.")
            )

        browse_row = QHBoxLayout()
        browse_row.addWidget(self._log_input)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_log_file)
        browse_row.addWidget(browse)
        layout.addLayout(browse_row)
        return page

    def _finish_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Ready to sync")
        layout = QVBoxLayout(page)
        outro = QLabel(
            "Click <b>Finish</b> to save your settings and start syncing. "
            "The 17Lands tray app will run in the background and start on login."
        )
        outro.setWordWrap(True)
        layout.addWidget(outro)
        page.setFinalPage(True)
        return page

    def _browse_log_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Select Arena Player.log",
            str(Path.home()),
            "Log files (*.log);;All files (*)",
        )
        if path:
            self._log_input.setText(path)

    def accept(self) -> None:
        token = self._token_input.text().strip()
        if not TOKEN_RE.match(token):
            QMessageBox.warning(
                self,
                APP_NAME,
                "Enter a valid 32-character token from 17lands.com/account.",
            )
            self.setCurrentId(1)
            return

        log_file = self._log_input.text().strip()
        if log_file and not Path(log_file).exists():
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "That log file does not exist yet. Save anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._settings.configure(token, log_file or None)
        super().accept()


def run_setup_wizard(parent: QWidget | None = None) -> bool:
    wizard = SetupWizard(parent)
    return wizard.exec() == QDialog.DialogCode.Accepted


class SettingsDialog(QDialog):
    def __init__(self, controller: SeventeenLandsTray, focus_token: bool = False) -> None:
        super().__init__(controller._window)
        self._controller = controller
        self._settings = ClientSettings.load()

        self.setWindowTitle(f"{APP_NAME} Settings")
        self.setWindowIcon(load_app_icon())
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Configure your 17Lands client token and optional Arena log path. "
            "Get your token from 17lands.com/account."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self._token_input = QLineEdit(self._settings.token or "")
        self._token_input.setPlaceholderText("32-character token from 17lands.com/account")
        self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Client token", self._token_input)

        log_row = QHBoxLayout()
        self._log_input = QLineEdit(self._settings.log_file or "")
        self._log_input.setPlaceholderText("Auto-detect if empty")
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._browse_log_file)
        log_row.addWidget(self._log_input)
        log_row.addWidget(browse_button)
        form.addRow("Arena log file", log_row)
        layout.addLayout(form)

        account_button = QPushButton("Open 17lands.com/account")
        account_button.clicked.connect(self._open_account_page)
        layout.addWidget(account_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if focus_token:
            self._token_input.setFocus()

    def _browse_log_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Select Arena Player.log",
            str(Path.home()),
            "Log files (*.log);;All files (*)",
        )
        if path:
            self._log_input.setText(path)

    def _open_account_page(self) -> None:
        subprocess.Popen(
            ["xdg-open", "https://www.17lands.com/account"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _save(self) -> None:
        token = self._token_input.text().strip()
        if not TOKEN_RE.match(token):
            QMessageBox.warning(
                self,
                APP_NAME,
                "Enter a valid 32-character token from 17lands.com/account.",
            )
            self._token_input.setFocus()
            return

        log_file = self._log_input.text().strip()
        if log_file and not Path(log_file).exists():
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "That log file does not exist yet. Save anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._settings.configure(token, log_file or None)
        self.accept()


class StatusWindow(QWidget):
    def __init__(self, controller: SeventeenLandsTray) -> None:
        super().__init__()
        self._controller = controller
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(420, 560)
        self.resize(460, 600)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self._status_banner = QLabel("Starting...")
        self._status_banner.setWordWrap(True)
        self._status_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_banner.setStyleSheet(
            "padding: 14px; border-radius: 10px; font-size: 15px; font-weight: 600;"
        )
        root.addWidget(self._status_banner)

        stats_frame = QFrame()
        stats_frame.setFrameShape(QFrame.Shape.StyledPanel)
        stats_layout = QVBoxLayout(stats_frame)
        stats_layout.setSpacing(6)

        self._account_label = QLabel()
        self._token_label = QLabel()
        self._log_label = QLabel()
        self._last_sync_label = QLabel()
        self._session_label = QLabel()
        self._activity_label = QLabel()

        for label in (
            self._account_label,
            self._token_label,
            self._log_label,
            self._last_sync_label,
            self._session_label,
            self._activity_label,
        ):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            stats_layout.addWidget(label)

        root.addWidget(stats_frame)

        recent_title = QLabel("Recent activity")
        recent_title.setStyleSheet("font-weight: 600;")
        root.addWidget(recent_title)

        self._events_list = QListWidget()
        self._events_list.setAlternatingRowColors(True)
        root.addWidget(self._events_list, stretch=1)

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(8)

        restart_button = QPushButton("Restart")
        restart_button.clicked.connect(self._controller.restart_follower)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self._controller.show_settings_dialog)
        log_button = QPushButton("Open log")
        log_button.clicked.connect(self._controller.open_log)
        site_button = QPushButton("17lands.com")
        site_button.clicked.connect(self._open_site)
        quit_button = QPushButton("Quit")
        quit_button.clicked.connect(self._controller.quit_app)
        quit_button.setStyleSheet("font-weight: 600;")

        buttons.addWidget(restart_button, 0, 0)
        buttons.addWidget(settings_button, 0, 1)
        buttons.addWidget(log_button, 1, 0)
        buttons.addWidget(site_button, 1, 1)
        buttons.addWidget(quit_button, 2, 0, 1, 2)
        root.addLayout(buttons)

        credit = QLabel(f'<a href="{CREDIT_URL}">GUI Tool Built By Greg.Tech</a>')
        credit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        credit.setOpenExternalLinks(True)
        credit.setStyleSheet("color: palette(mid); font-size: 11px; padding-top: 4px;")
        root.addWidget(credit)

    def _open_site(self) -> None:
        subprocess.Popen(
            ["xdg-open", "https://www.17lands.com/"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()

    def update_snapshot(self, snapshot: ClientSnapshot) -> None:
        fg, bg = STATE_STYLES.get(snapshot.state, STATE_STYLES["idle"])
        self._status_banner.setText(snapshot.state_detail)
        self._status_banner.setStyleSheet(
            f"padding: 14px; border-radius: 10px; font-size: 15px; font-weight: 600;"
            f"color: {fg}; background: {bg};"
        )

        self._account_label.setText(
            f"Account: {snapshot.account or 'Unknown (launch Arena to detect)'}"
        )
        settings = ClientSettings.load()
        self._token_label.setText(f"Token: {settings.masked_token()}")
        configured_log = settings.log_file or snapshot.arena_log
        self._log_label.setText(
            f"Arena log: {short_log_path(configured_log)}"
        )
        self._last_sync_label.setText(
            f"Last upload: {format_relative(snapshot.last_sync_at)}"
        )
        self._session_label.setText(
            f"Session uploads: {snapshot.session_uploads} match"
            f"{'' if snapshot.session_uploads == 1 else 'es'}"
        )
        self._activity_label.setText(
            f"Last client activity: {format_relative(snapshot.last_event_at)}"
        )

        self._events_list.clear()
        if snapshot.recent_events:
            for event in snapshot.recent_events:
                time_text = event.timestamp.strftime("%H:%M:%S")
                item = QListWidgetItem(f"{time_text}  {event.summary}")
                if event.level == "ERROR":
                    item.setForeground(QColor("#b42318"))
                elif event.level == "WARNING":
                    item.setForeground(QColor("#9a6700"))
                self._events_list.addItem(item)
        else:
            self._events_list.addItem("No parsed activity yet")


class SeventeenLandsTray(QSystemTrayIcon):
    def __init__(self, *, defer_start: bool = False) -> None:
        super().__init__()
        self._process = QProcess()
        self._process.setProgram(SEVENTEENLANDS_CMD)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.finished.connect(self._on_process_finished)
        self._process.started.connect(self._on_process_started)

        self._restart_timer = QTimer()
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self.start_follower)

        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(STATUS_POLL_MS)

        self._auto_restart = True
        self._starting = True
        self._external_process = False
        self._snapshot = ClientSnapshot(state="starting", state_detail="Starting...")
        self._tray_dbus_status = TrayDBusStatus()

        (
            self._icon_running,
            self._icon_stopped,
            self._icon_starting,
            self._icon_syncing,
        ) = load_icon_variants()

        self._menu_host = QWidget()
        self._menu_host.hide()

        self._window = StatusWindow(self)
        self._menu = self._build_menu()
        self._menu.setParent(self._menu_host)
        self._menu.setTitle(APP_NAME)
        self.activated.connect(self._on_activated)

        app_icon = load_app_icon()
        self.setIcon(app_icon)
        self.setToolTip(APP_NAME)
        self._apply_snapshot(self._snapshot, self._icon_starting)
        self.show()
        if not defer_start:
            self.start_follower()
        QTimer.singleShot(1500, self._refresh_tray_relevance)

    def _refresh_tray_relevance(self) -> None:
        self._tray_dbus_status.reset()
        self._apply_tray_presence(self._snapshot.state)

    def _apply_tray_presence(self, state: str) -> None:
        # Best-effort Passive/Active for KDE "show when relevant". Qt does not
        # reliably expose StatusNotifierItem status, so never hide() the tray icon
        # while the app is running — that unregisters it and makes the app look dead.
        self._tray_dbus_status.set_for_state(state)
        if not self.isVisible():
            self.show()

    def _build_menu(self) -> QMenu:
        menu = QMenu()

        show_action = QAction("Show status")
        show_action.triggered.connect(self.show_status_window)
        menu.addAction(show_action)

        start_action = QAction("Start")
        start_action.triggered.connect(self.start_follower)
        menu.addAction(start_action)

        stop_action = QAction("Stop")
        stop_action.triggered.connect(self.stop_follower)
        menu.addAction(stop_action)

        restart_action = QAction("Restart")
        restart_action.triggered.connect(self.restart_follower)
        menu.addAction(restart_action)

        menu.addSeparator()

        setup_action = QAction("Run setup wizard…")
        setup_action.triggered.connect(self.show_setup_wizard)
        menu.addAction(setup_action)

        settings_action = QAction("Settings…")
        settings_action.triggered.connect(self.show_settings_dialog)
        menu.addAction(settings_action)

        reauth_action = QAction("Re-authenticate…")
        reauth_action.triggered.connect(self.show_reauthenticate_dialog)
        menu.addAction(reauth_action)

        menu.addSeparator()

        log_action = QAction("View log file")
        log_action.triggered.connect(self.open_log)
        menu.addAction(log_action)

        site_action = QAction("Open 17lands.com")
        site_action.triggered.connect(
            lambda: subprocess.Popen(
                ["xdg-open", "https://www.17lands.com/"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        menu.addAction(site_action)

        menu.addSeparator()

        quit_action = QAction("Quit")
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        return menu

    def show_tray_menu(self, global_pos=None) -> None:
        if global_pos is None:
            global_pos = QCursor.pos()
        self._menu.exec(global_pos)

    def show_setup_wizard(self) -> None:
        if run_setup_wizard(self._window):
            self.restart_follower()
            self.show_status_window()

    def show_settings_dialog(self) -> None:
        dialog = SettingsDialog(self, focus_token=False)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.restart_follower()
            self.show_status_window()

    def show_reauthenticate_dialog(self) -> None:
        dialog = SettingsDialog(self, focus_token=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.restart_follower()
            self.show_status_window()

    def handle_command(self, command: str) -> None:
        command = command.strip().lower()
        if command == SETUP_COMMAND:
            self.show_setup_wizard()
        elif command in {"reauthenticate", "reauth"}:
            self.show_reauthenticate_dialog()
        elif command in {"settings", "authenticate"}:
            self.show_settings_dialog()
        elif command == "show":
            self.show_status_window()
        elif command == "quit":
            self.quit_app()

    def show_status_window(self) -> None:
        self._refresh_status()
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Context:
            self.show_tray_menu()
            return

        if reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.show_settings_dialog()
            return

        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_status_window()

    def _process_running(self) -> bool:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            return True
        return bool(self._find_other_seventeenlands_pids())

    def _apply_snapshot(self, snapshot: ClientSnapshot, icon: QIcon) -> None:
        self._snapshot = snapshot
        self.setIcon(icon)
        self.setToolTip(f"{APP_NAME}\n{snapshot.state_detail}")
        self._apply_tray_presence(snapshot.state)
        if self._window.isVisible():
            self._window.update_snapshot(snapshot)

    def _icon_for_snapshot(self, snapshot: ClientSnapshot) -> QIcon:
        if snapshot.state == "syncing":
            return self._icon_syncing
        if snapshot.state in {"up_to_date", "watching"}:
            return self._icon_running
        if snapshot.state == "idle":
            return self._icon_starting
        if snapshot.state == "stopped":
            return self._icon_stopped
        return self._icon_starting

    def _refresh_status(self) -> None:
        process_running = self._process_running()
        external = self._external_process or (
            process_running and self._process.state() == QProcess.ProcessState.NotRunning
        )
        snapshot = LogAnalyzer.build_snapshot(
            process_running=process_running,
            external_process=external,
            starting=self._starting and process_running,
        )
        if process_running and not self._starting:
            pass
        elif not process_running:
            self._starting = False

        icon = self._icon_for_snapshot(snapshot)
        self._apply_snapshot(snapshot, icon)

    def _find_other_seventeenlands_pids(self) -> list[int]:
        result = subprocess.run(
            ["pgrep", "-f", r"/seventeenlands$"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids = [int(pid) for pid in result.stdout.split() if pid.strip()]
        our_pid = self._process.processId()
        if our_pid > 0:
            pids = [pid for pid in pids if pid != our_pid]
        return pids

    def start_follower(self) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            return

        existing = self._find_other_seventeenlands_pids()
        if existing:
            self._external_process = True
            self._starting = False
            self._refresh_status()
            return

        self._external_process = False

        if not Path(SEVENTEENLANDS_CMD).exists():
            self._auto_restart = False
            self._starting = False
            snapshot = ClientSnapshot(
                state="error",
                state_detail=f"Missing client: {SEVENTEENLANDS_CMD}",
                process_running=False,
            )
            self._apply_snapshot(snapshot, self._icon_stopped)
            self.showMessage(
                APP_NAME,
                f"Could not find seventeenlands at {SEVENTEENLANDS_CMD}",
                QSystemTrayIcon.MessageIcon.Critical,
            )
            return

        self._starting = True
        self._refresh_status()
        self._apply_process_arguments()
        self._process.start()

    def _apply_process_arguments(self) -> None:
        settings = ClientSettings.load()
        args: list[str] = []
        if settings.log_file:
            args.extend(["-l", settings.log_file])
        if settings.token:
            args.extend(["--token", settings.token])
        self._process.setArguments(args)

    def stop_follower(self) -> None:
        self._auto_restart = False
        self._restart_timer.stop()
        self._starting = False
        self._external_process = False

        if self._process.state() == QProcess.ProcessState.NotRunning:
            for pid in self._find_other_seventeenlands_pids():
                os.kill(pid, signal.SIGTERM)
            self._refresh_status()
            return

        self._process.terminate()
        if not self._process.waitForFinished(3000):
            self._process.kill()
            self._process.waitForFinished(1000)

        self._refresh_status()

    def restart_follower(self) -> None:
        self._auto_restart = True
        self._starting = True
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.finished.disconnect(self._on_process_finished)
            self.stop_follower()
            self._process.finished.connect(self._on_process_finished)
        else:
            for pid in self._find_other_seventeenlands_pids():
                os.kill(pid, signal.SIGTERM)
        self.start_follower()

    def _on_process_started(self) -> None:
        self._starting = False
        self._refresh_status()

    def _on_process_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        self._starting = False
        if not self._auto_restart:
            self._refresh_status()
            return

        snapshot = ClientSnapshot(
            state="starting",
            state_detail=f"Crashed (exit {exit_code}), restarting...",
            process_running=False,
        )
        self._apply_snapshot(snapshot, self._icon_starting)
        self._restart_timer.start(RESTART_DELAY_MS)

    def open_log(self) -> None:
        if LOG_FILE.exists():
            subprocess.Popen(
                ["xdg-open", str(LOG_FILE)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            self.showMessage(
                APP_NAME,
                f"Log file not found:\n{LOG_FILE}",
                QSystemTrayIcon.MessageIcon.Information,
            )

    def quit_app(self) -> None:
        self._auto_restart = False
        self._restart_timer.stop()
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(3000):
                self._process.kill()
        for pid in self._find_other_seventeenlands_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        self._window.close()
        QApplication.instance().quit()


def quit_running_instance() -> int:
    tray_result = subprocess.run(
        ["pgrep", "-f", r"seventeenlands-tray"],
        capture_output=True,
        text=True,
        check=False,
    )
    client_result = subprocess.run(
        ["pgrep", "-f", r"/seventeenlands$"],
        capture_output=True,
        text=True,
        check=False,
    )

    current_pid = os.getpid()
    tray_pids = [int(pid) for pid in tray_result.stdout.split() if pid.strip()]
    tray_pids = [pid for pid in tray_pids if pid != current_pid]
    client_pids = [int(pid) for pid in client_result.stdout.split() if pid.strip()]

    if not tray_pids and not client_pids:
        print("17Lands tray is not running.")
        return 1

    for pid in tray_pids:
        os.kill(pid, signal.SIGTERM)

    if tray_pids:
        time.sleep(1)

    for pid in client_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    print("Stopped 17Lands tray.")
    return 0


def main() -> int:
    if "--quit" in sys.argv:
        return quit_running_instance()

    if "--setup" in sys.argv:
        sys.argv.remove("--setup")
        if send_instance_command(SETUP_COMMAND):
            return 0
        os.environ[OPEN_SETTINGS_ENV] = SETUP_COMMAND

    settings_commands = {"--settings", "--authenticate", "--reauthenticate", "--reauth"}
    requested = settings_commands.intersection(sys.argv)
    if requested:
        command = next(iter(requested)).removeprefix("--")
        remote_command = "reauthenticate" if command == "reauth" else command
        if send_instance_command(remote_command):
            return 0
        os.environ[OPEN_SETTINGS_ENV] = remote_command

    skip_detach = (
        os.environ.get(OPEN_SETTINGS_ENV) == SETUP_COMMAND
        or "--no-detach" in sys.argv
    )
    if "--no-detach" in sys.argv:
        sys.argv.remove("--no-detach")
    if not skip_detach:
        detach_from_terminal()

    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(APP_NAME)
    QApplication.setDesktopFileName(APP_ID)
    QApplication.setQuitOnLastWindowClosed(False)

    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())

    def on_command(command: str) -> None:
        if hasattr(app, "_tray"):
            app._tray.handle_command(command)

    try:
        lock = SingleInstance(APP_ID, on_command=on_command)
    except RuntimeError:
        return 0

    pending = os.environ.pop(OPEN_SETTINGS_ENV, None)
    need_setup = pending == SETUP_COMMAND or (
        pending is None and not ClientSettings.is_configured()
    )
    tray = SeventeenLandsTray(defer_start=need_setup)
    app._tray = tray

    if pending == SETUP_COMMAND or (need_setup and pending is None):
        if not run_setup_wizard(tray._window):
            print("Setup cancelled.")
            tray.quit_app()
            return 1
        tray.start_follower()
    elif pending == "reauthenticate":
        QTimer.singleShot(0, tray.show_reauthenticate_dialog)
    elif pending:
        QTimer.singleShot(0, tray.show_settings_dialog)

    def cleanup(_signum: int, _frame: object) -> None:
        tray.quit_app()

    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    exit_code = app.exec()
    lock.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
