#!/usr/bin/env python3
"""Lightweight system tray wrapper that keeps seventeenlands running."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QProcess, QRectF, QSocketNotifier, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPalette,
    QPixmap,
)
from PyQt6.QtNetwork import (
    QLocalServer,
    QLocalSocket,
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

__version__ = "1.2.1"

APP_ID = "seventeenlands-tray"
APP_NAME = "17Lands"
DETACH_ENV = "SEVENTEENLANDS_TRAY_DETACHED"

SITE_URL = "https://www.17lands.com/"
ACCOUNT_URL = "https://www.17lands.com/account"
CREDIT_URL = "https://greg.tech"
REPO_SLUG = "grpace/17Lands-Linux"
INSTALL_URL = f"https://github.com/{REPO_SLUG}/raw/main/install.sh"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO_SLUG}/main/seventeenlands_tray.py"
SERVICE_NAME = "seventeenlands-tray.service"
UPDATE_CHECK_DELAY_MS = 30_000
UPDATE_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000

LOG_FILE = Path.home() / ".seventeenlands" / "seventeenlands.log"
CONFIG_FILE = Path.home() / ".mtga_follower.ini"
TRAY_SETTINGS_DIR = Path.home() / ".config" / "seventeenlands-tray"
TRAY_SETTINGS_FILE = TRAY_SETTINGS_DIR / "settings.ini"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / APP_ID
UPDATE_LOG = STATE_DIR / "update.log"
UPDATE_MARKER = STATE_DIR / "update-pending"
REPAIR_LOG = STATE_DIR / "client-repair.log"

RESTART_DELAY_MS = 5_000
MAX_RESTART_DELAY_MS = 300_000
STABLE_RUN_SECONDS = 60
STATUS_POLL_MS = 2_000
SYNC_ACTIVE_SECONDS = 12
IDLE_SECONDS = 300
CATCH_UP_SECONDS = 120
ERROR_WINDOW_SECONDS = 600
LOG_TAIL_BYTES = 120_000
MAX_RECENT_EVENTS = 20
CLIENT_OUTPUT_LINES = 50

LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{8} \d{6}\.\d+),(?P<level>\w+),(?P<logger>[^,]+),(?P<message>.+)$"
)
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
PYTHON_RE = re.compile(r"^python[\d.]*$")

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


# --- Paths and environment -------------------------------------------------


def resolve_runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime and Path(runtime).is_dir():
        return Path(runtime)
    fallback = Path(tempfile.gettempdir()) / f"{APP_ID}-{os.getuid()}"
    fallback.mkdir(mode=0o700, exist_ok=True)
    return fallback


SOCKET_PATH = resolve_runtime_dir() / f"{APP_ID}.sock"


def client_command() -> str:
    """Locate the seventeenlands executable (resolved on each start)."""
    return shutil.which("seventeenlands") or str(Path.home() / ".local/bin/seventeenlands")


def seventeenlands_installed() -> bool:
    return Path(client_command()).exists()


def open_url(url: str | QUrl) -> None:
    QDesktopServices.openUrl(url if isinstance(url, QUrl) else QUrl(url))


# --- Process discovery (own user only, via /proc) ----------------------------


def _program_names(argv: list[str]) -> set[str]:
    """Basename of the program, plus the script name when run via an interpreter."""
    names = {Path(argv[0]).name}
    if len(argv) > 1 and PYTHON_RE.match(Path(argv[0]).name):
        names.add(Path(argv[1]).name)
    return names


def is_client_argv(argv: list[str]) -> bool:
    return "seventeenlands" in _program_names(argv)


def is_tray_argv(argv: list[str]) -> bool:
    return bool({"seventeenlands-tray", "seventeenlands_tray.py"} & _program_names(argv))


def find_pids(match: Callable[[list[str]], bool]) -> list[int]:
    uid = os.getuid()
    own_pid = os.getpid()
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if argv and match(argv):
            pids.append(int(entry.name))
    return pids


def pid_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


def wait_for_exit(pids: list[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + timeout
    remaining = [pid for pid in pids if pid_alive(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [pid for pid in remaining if pid_alive(pid)]
    return remaining


def terminate_pids(pids: list[int], timeout: float = 3.0) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    for pid in wait_for_exit(pids, timeout):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


# --- Settings ---------------------------------------------------------------


def _write_config(path: Path, config: configparser.ConfigParser, *, private: bool) -> None:
    """Write atomically; private files are created with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as handle:
        config.write(handle)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_config(path: Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if path.exists():
        with contextlib.suppress(configparser.Error):
            config.read(path)
    if "client" not in config:
        config["client"] = {}
    return config


@dataclass
class ClientSettings:
    token: str | None = None
    log_file: str | None = None

    @classmethod
    def load(cls) -> ClientSettings:
        settings = cls()
        token = _read_config(CONFIG_FILE)["client"].get("token", "").strip()
        if TOKEN_RE.match(token):
            settings.token = token
        log_file = _read_config(TRAY_SETTINGS_FILE)["client"].get("log_file", "").strip()
        settings.log_file = log_file or None
        return settings

    @classmethod
    def is_configured(cls) -> bool:
        return cls.load().token is not None

    def save_token(self, token: str) -> None:
        # Shared with the upstream client, which reads its token from this file.
        config = _read_config(CONFIG_FILE)
        config["client"]["token"] = token
        _write_config(CONFIG_FILE, config, private=True)
        self.token = token

    def save_log_file(self, log_file: str | None) -> None:
        config = _read_config(TRAY_SETTINGS_FILE)
        if log_file:
            config["client"]["log_file"] = log_file
        else:
            config["client"].pop("log_file", None)
        _write_config(TRAY_SETTINGS_FILE, config, private=False)
        self.log_file = log_file

    def configure(self, token: str, log_file: str | None) -> None:
        self.save_token(token)
        self.save_log_file(log_file)

    def masked_token(self) -> str:
        if not self.token:
            return "Not configured"
        return f"{self.token[:4]}...{self.token[-4:]}"


def detect_arena_log_paths() -> list[Path]:
    username = os.environ.get("USER") or os.environ.get("LOGNAME") or "steamuser"
    mtga = "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
    steam_prefix = "steamapps/compatdata/2141910/pfx/drive_c/users/steamuser"
    candidates = [
        Path.home() / ".local/share/Steam" / steam_prefix / mtga,
        Path.home() / ".steam/steam" / steam_prefix / mtga,
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam" / steam_prefix / mtga,
        Path.home() / f"Games/magic-the-gathering-arena/drive_c/users/{username}" / mtga,
        Path.home() / f".wine/drive_c/users/{username}" / mtga,
    ]
    seen: set[Path] = set()
    found: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            found.append(path)
    return found


# --- Log analysis -----------------------------------------------------------


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime
    level: str
    message: str


@dataclass
class ParsedEvent:
    timestamp: datetime
    level: str
    summary: str
    kind: str = "info"


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
    _cache_key: tuple[int, int, int] | None = None
    _cache_entries: list[LogEntry] = []

    @staticmethod
    def read_tail(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> list[str]:
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
    def parse_lines(cls, lines: list[str]) -> list[LogEntry]:
        entries: list[LogEntry] = []
        for line in lines:
            match = LOG_LINE_RE.match(line)
            if not match:
                continue
            ts = cls.parse_timestamp(match.group("ts"))
            if ts is not None:
                entries.append(LogEntry(ts, match.group("level"), match.group("message")))
        return entries

    @classmethod
    def read_entries(cls, path: Path | None = None) -> list[LogEntry]:
        """Parse the log tail, re-reading only when the file has changed."""
        path = path or LOG_FILE
        try:
            stat = path.stat()
        except OSError:
            cls._cache_key, cls._cache_entries = None, []
            return []
        key = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if key != cls._cache_key:
            cls._cache_entries = cls.parse_lines(cls.read_tail(path))
            cls._cache_key = key
        return cls._cache_entries

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

        if "Human draft pick" in message or message.startswith("Draft pick:"):
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
    def classify_message(message: str, level: str) -> str:
        """Bucket a log message for the activity list's colored markers."""
        if level == "ERROR":
            return "error"
        if level == "WARNING" or "Detailed logs are disabled" in message:
            return "warning"
        if "draft pick" in message.lower() or message.startswith("Joined draft pod:"):
            return "draft"
        if message.startswith("Added pending match result"):
            return "win" if "'won_match': True" in message else "loss"
        if message.startswith("Completed game:"):
            return "match"
        if message.startswith(("Submitting", "Adding game history")):
            return "upload"
        if message.startswith("Deck submission"):
            return "deck"
        return "info"

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
        session_start: datetime | None = None,
        entries: list[LogEntry] | None = None,
        now: datetime | None = None,
    ) -> ClientSnapshot:
        entries = cls.read_entries() if entries is None else entries
        now = now or datetime.now()
        error_cutoff = now - timedelta(seconds=ERROR_WINDOW_SECONDS)
        if session_start and session_start > error_cutoff:
            error_cutoff = session_start

        recent_events: list[ParsedEvent] = []
        account: str | None = None
        arena_log: str | None = None
        last_event_at: datetime | None = None
        last_sync_at: datetime | None = None
        session_uploads = 0
        has_error = False
        waiting_for_log = False
        recent_sync_activity = False

        for entry in entries:
            ts, level, message = entry.timestamp, entry.level, entry.message
            last_event_at = ts

            if level == "ERROR" and ts >= error_cutoff:
                has_error = True

            if message.startswith("Following along "):
                arena_log = message.split("Following along ", 1)[1].strip()
                waiting_for_log = False
            elif message.startswith("Found no files to parse"):
                waiting_for_log = True

            screen_name = cls._extract_screen_name(message)
            if screen_name:
                account = screen_name

            if message.startswith("Submitting queued game result"):
                last_sync_at = ts
                if session_start is None or ts >= session_start:
                    session_uploads += 1

            if ts >= now - timedelta(seconds=SYNC_ACTIVE_SECONDS) and any(
                keyword in message for keyword in SYNC_KEYWORDS
            ):
                recent_sync_activity = True

            summary = cls.summarize_message(message)
            if summary:
                kind = cls.classify_message(message, level)
                recent_events.append(ParsedEvent(ts, level, summary, kind))

        recent_events = recent_events[-MAX_RECENT_EVENTS:]
        recent_events.reverse()

        just_started = session_start is not None and now - session_start <= timedelta(
            seconds=CATCH_UP_SECONDS
        )
        if not process_running:
            state = "stopped"
        elif starting:
            state = "starting"
        elif has_error:
            state = "error"
        elif waiting_for_log:
            state = "waiting"
        elif recent_sync_activity:
            state = "catching_up" if just_started else "syncing"
        elif last_event_at and now - last_event_at <= timedelta(seconds=IDLE_SECONDS):
            recent_sync = last_sync_at and now - last_sync_at <= timedelta(minutes=30)
            state = "up_to_date" if recent_sync else "watching"
        else:
            state = "idle"

        state_detail = STATE_DETAILS[state]
        if external_process and process_running:
            state_detail += " Using a separately started client."

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


# --- Self-update -------------------------------------------------------------


VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)


def parse_version(text: str) -> tuple[int, ...] | None:
    """Extract __version__ from source text as a comparable tuple."""
    match = VERSION_RE.search(text)
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def update_script(log_file: Path) -> str:
    """Shell script that runs the installer and always brings the tray back up.

    SEVENTEENLANDS_TRAY_INSTALLER can point at a local install.sh (for testing).
    """
    installer = os.environ.get("SEVENTEENLANDS_TRAY_INSTALLER", "").strip()
    if installer:
        run = f"bash {shlex.quote(installer)}"
    else:
        run = f"curl -fsSL {shlex.quote(INSTALL_URL)} | bash"
    log = shlex.quote(str(log_file))
    return (
        "set -o pipefail\n"
        f'{{ echo "== 17Lands update started $(date)"; {run}; }} > {log} 2>&1\n'
        "rc=$?\n"
        f'echo "== Finished with exit code $rc" >> {log}\n'
        f"systemctl --user start {SERVICE_NAME} >/dev/null 2>&1 || true\n"
        "exit $rc\n"
    )


def launch_update() -> bool:
    """Run the installer outside our own process tree, so it survives the
    installer stopping this tray app (systemd stops the whole service cgroup)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    UPDATE_MARKER.write_text(__version__)
    script = update_script(UPDATE_LOG)
    if shutil.which("systemd-run"):
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--collect",
                "--quiet",
                f"--unit=seventeenlands-tray-update-{int(time.time())}",
                "--description=17Lands tray update",
                "/bin/bash",
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return True
    try:
        subprocess.Popen(
            ["/bin/bash", "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        UPDATE_MARKER.unlink(missing_ok=True)
        return False


def read_update_result() -> tuple[str, bool] | None:
    """After an update restart: (previous version, succeeded), once."""
    try:
        previous = UPDATE_MARKER.read_text().strip()
    except OSError:
        return None
    UPDATE_MARKER.unlink(missing_ok=True)
    try:
        last = UPDATE_LOG.read_text(errors="replace").strip().splitlines()[-1]
    except (OSError, IndexError):
        last = ""
    return previous, last.endswith("exit code 0")


def client_install_broken(output: list[str]) -> bool:
    """True when the client died on an import error, which is what happens to a
    pip --user (or pipx) install after the system Python's minor version changes."""
    return any("ModuleNotFoundError" in line or "ImportError" in line for line in output[-5:])


def repair_script(log_file: Path) -> str:
    """Shell script that reinstalls the seventeenlands client for the current
    python3, the way install.sh does, without sudo. Output goes to log_file."""
    log = shlex.quote(str(log_file))
    return (
        "set -o pipefail\n"
        f"mkdir -p {shlex.quote(str(log_file.parent))}\n"
        "{\n"
        '  echo "== 17Lands client repair started $(date) ($(python3 --version 2>&1))"\n'
        '  client=$(command -v seventeenlands || echo "$HOME/.local/bin/seventeenlands")\n'
        '  target=$(readlink -f "$client")\n'
        '  if command -v pipx >/dev/null 2>&1 && [[ "$target" == */pipx/venvs/* ]]; then\n'
        "    pipx reinstall seventeenlands || pipx install --force seventeenlands\n"
        "  elif python3 -m pip install --user --upgrade seventeenlands; then\n"
        "    :\n"
        "  elif command -v pipx >/dev/null 2>&1; then\n"
        "    pipx install --force seventeenlands\n"
        "  else\n"
        "    false\n"
        "  fi\n"
        "  rc=$?\n"
        '  echo "== Finished with exit code $rc"\n'
        "  exit $rc\n"
        f"}} > {log} 2>&1\n"
    )


# --- Single instance + remote commands (per-user Unix socket) ----------------


class InstanceServer(QObject):
    """Accept one-line commands from other invocations of the tray app."""

    def __init__(self, on_command: Callable[[str], None]) -> None:
        super().__init__()
        self._on_command = on_command
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new_connection)

    def listen(self) -> bool:
        """Claim the socket; False only if another live instance already owns it."""
        if send_instance_command("ping"):
            return False
        QLocalServer.removeServer(str(SOCKET_PATH))
        if not self._server.listen(str(SOCKET_PATH)):
            # Still run; we just can't receive --settings/--quit style commands.
            print(
                f"Warning: cannot listen on {SOCKET_PATH}: {self._server.errorString()}",
                file=sys.stderr,
            )
        return True

    def _on_new_connection(self) -> None:
        while (connection := self._server.nextPendingConnection()) is not None:
            connection.readyRead.connect(lambda c=connection: self._read(c))
            connection.disconnected.connect(connection.deleteLater)
            self._read(connection)

    def _read(self, connection: QLocalSocket) -> None:
        if not connection.canReadLine():
            return
        command = bytes(connection.readLine()).decode("utf-8", errors="replace").strip()
        connection.disconnectFromServer()
        if command:
            self._on_command(command)

    def close(self) -> None:
        self._server.close()


def send_instance_command(command: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(str(SOCKET_PATH))
            sock.sendall(f"{command}\n".encode())
        return True
    except OSError:
        return False


# --- Icons ------------------------------------------------------------------


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


BADGE_TOP = "#5c7cfa"
BADGE_BOTTOM = "#3b5bdb"


def make_badge_icon(size: int = 256) -> QIcon:
    """The app icon: a white "17" on a rounded blue badge (matches the SVG asset)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    margin = size * 0.04
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
    gradient.setColorAt(0, QColor(BADGE_TOP))
    gradient.setColorAt(1, QColor(BADGE_BOTTOM))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(rect, size * 0.22, size * 0.22)

    font = QFont("DejaVu Sans")
    font.setBold(True)
    font.setPixelSize(int(size * 0.5))
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "17")
    painter.end()
    return QIcon(pixmap)


def load_app_icon() -> QIcon:
    themed = QIcon.fromTheme(APP_ID)
    return themed if not themed.isNull() else make_badge_icon()


# Tray icon colors (the tray keeps the plain "17" glyph).
ICON_COLORS = {
    "running": "#f2f2f2",
    "stopped": "#5c5c5c",
    "starting": "#9a9a9a",
    "syncing": "#6ec6ff",
    "error": "#ff6b5e",
}

STATE_ICONS = {
    "up_to_date": "running",
    "watching": "running",
    "syncing": "syncing",
    "catching_up": "syncing",
    "stopped": "stopped",
    "error": "error",
    "restarting": "error",
    "repairing": "syncing",
}

STATE_HEADLINES = {
    "up_to_date": "Up to Date",
    "watching": "Watching for Games",
    "syncing": "Syncing",
    "catching_up": "Catching Up",
    "starting": "Starting",
    "restarting": "Restarting",
    "repairing": "Repairing Client",
    "waiting": "Waiting for Arena",
    "idle": "Idle",
    "stopped": "Stopped",
    "error": "Needs Attention",
}

STATE_DETAILS = {
    "up_to_date": "Your latest games are uploaded.",
    "watching": "Watching Arena for new drafts and matches.",
    "syncing": "Uploading your latest Arena data.",
    "catching_up": "Re-reading your Arena log after startup. This is normal and "
    "finishes on its own.",
    "starting": "Starting the 17Lands client…",
    "waiting": "Can't find Arena's Player.log yet. Launch Arena, or set the log path in Settings.",
    "idle": "No recent Arena activity. Uploads resume automatically when you play.",
    "stopped": "The 17Lands client isn't running, so nothing is uploading.",
    "error": "The client logged errors recently. Open the log for details.",
}

STATE_ACCENTS = {
    "up_to_date": "#2f9e44",
    "watching": "#2f9e44",
    "syncing": "#1c7ed6",
    "catching_up": "#1c7ed6",
    "starting": "#e67700",
    "restarting": "#e67700",
    "repairing": "#1c7ed6",
    "waiting": "#e67700",
    "idle": "#868e96",
    "stopped": "#e03131",
    "error": "#e03131",
}

KIND_COLORS = {
    "draft": "#7048e8",
    "win": "#2f9e44",
    "loss": "#e03131",
    "match": "#868e96",
    "upload": "#1c7ed6",
    "deck": "#e67700",
    "warning": "#f59f00",
    "error": "#e03131",
    "info": "#adb5bd",
}

_dot_cache: dict[str, QIcon] = {}


def dot_icon(color: str, size: int = 10) -> QIcon:
    if color not in _dot_cache:
        pixmap = QPixmap(size * 2, size * 2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(size * 0.5, size * 0.5, size, size))
        painter.end()
        _dot_cache[color] = QIcon(pixmap)
    return _dot_cache[color]


def mix(a: QColor, b: QColor, amount: float) -> QColor:
    """Blend b into a by amount (0 = a, 1 = b)."""
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * amount,
        a.greenF() + (b.greenF() - a.greenF()) * amount,
        a.blueF() + (b.blueF() - a.blueF()) * amount,
    )


def theme_icon(*names: str) -> QIcon:
    for name in names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    return QIcon()


# --- Formatting helpers -----------------------------------------------------


def format_relative(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "Never"
    seconds = int(((now or datetime.now()) - when).total_seconds())
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hr ago"
    days = seconds // 86400
    return f"{days} day{'' if days == 1 else 's'} ago"


def format_event_time(when: datetime, now: datetime | None = None) -> str:
    today = (now or datetime.now()).date()
    if when.date() == today:
        return f"{when:%H:%M}"
    if when.date() == today - timedelta(days=1):
        return f"Yesterday {when:%H:%M}"
    return f"{when:%b} {when.day} {when:%H:%M}"


def link(url: str, text: str, color: QColor | None = None, underline: bool = True) -> str:
    """Rich-text link that follows the theme instead of Qt's default bright blue."""
    color = color or QApplication.palette().color(QPalette.ColorRole.WindowText)
    decoration = "underline" if underline else "none"
    return (
        f'<a href="{url}" style="color: {color.name()}; text-decoration: {decoration};">{text}</a>'
    )


def short_log_path(path: str | None) -> str:
    if not path:
        return "Not detected yet"
    name = Path(path).name
    parent = Path(path).parent.as_posix().lower()
    if "steam" in parent:
        return f"{name} (Steam)"
    if "lutris" in parent or "games/" in parent:
        return f"{name} (Lutris)"
    if "wine" in parent:
        return f"{name} (Wine)"
    return name


# --- Dialogs ----------------------------------------------------------------


def browse_for_log(parent: QWidget, target: QLineEdit) -> None:
    path, _filter = QFileDialog.getOpenFileName(
        parent,
        "Select Arena Player.log",
        target.text() or str(Path.home()),
        "Log files (*.log);;All files (*)",
    )
    if path:
        target.setText(path)


def validate_and_save(parent: QWidget, token: str, log_file: str) -> bool:
    """Validate user input, confirm odd choices, and persist. False = keep editing."""
    if not TOKEN_RE.match(token):
        QMessageBox.warning(
            parent, APP_NAME, "Enter a valid 32-character token from 17lands.com/account."
        )
        return False
    if log_file and not Path(log_file).exists():
        answer = QMessageBox.question(
            parent,
            APP_NAME,
            "That log file does not exist yet. Save anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
    ClientSettings().configure(token, log_file or None)
    return True


def make_token_input(settings: ClientSettings) -> QLineEdit:
    line = QLineEdit(settings.token or "")
    line.setPlaceholderText("Paste your 32-character token here")
    line.setEchoMode(QLineEdit.EchoMode.Password)
    line.setClearButtonEnabled(True)
    return line


def token_row(line: QLineEdit) -> QHBoxLayout:
    """The token field plus a Show/Hide toggle."""
    row = QHBoxLayout()
    row.addWidget(line, stretch=1)
    toggle = QPushButton("Show")
    toggle.setCheckable(True)

    def on_toggled(checked: bool) -> None:
        line.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        toggle.setText("Hide" if checked else "Show")

    toggle.toggled.connect(on_toggled)
    row.addWidget(toggle)
    return row


class _TokenPage(QWizardPage):
    def __init__(self, token_input: QLineEdit) -> None:
        super().__init__()
        self._token_input = token_input

    def validatePage(self) -> bool:  # noqa: N802
        if TOKEN_RE.match(self._token_input.text().strip()):
            return True
        QMessageBox.warning(
            self, APP_NAME, "Enter a valid 32-character token from 17lands.com/account."
        )
        return False


class SetupWizard(QWizard):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} Setup")
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(520, 420)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)

        settings = ClientSettings.load()
        self._token_input = make_token_input(settings)
        self._log_input = QLineEdit(settings.log_file or "")
        self._log_input.setPlaceholderText("Auto-detect if empty")

        self.addPage(self._welcome_page())
        self.addPage(self._token_page())
        self.addPage(self._log_page())
        self.addPage(self._finish_page())

    def _welcome_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Welcome to 17Lands")
        layout = QVBoxLayout(page)

        status = "found" if seventeenlands_installed() else "not found — re-run the installer"
        intro = QLabel(
            "<p>This setup will connect 17Lands to your MTGA account so draft and "
            "match data sync automatically.</p>"
            f"<p><b>seventeenlands client:</b> {status}</p>"
            f"<p>You will need a free account at {link(SITE_URL, '17lands.com')}.</p>"
        )
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        layout.addWidget(intro)
        return page

    def _token_page(self) -> QWizardPage:
        page = _TokenPage(self._token_input)
        page.setTitle("Sign In to 17Lands")
        layout = QVBoxLayout(page)

        intro = QLabel(
            f"Copy your client token from {link(ACCOUNT_URL, '17lands.com/account')} "
            "and paste it below."
        )
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        layout.addWidget(intro)
        layout.addLayout(token_row(self._token_input))

        open_account = QPushButton("Open 17lands.com/account in Browser")
        open_account.clicked.connect(lambda: open_url(ACCOUNT_URL))
        layout.addWidget(open_account)
        page.registerField("token*", self._token_input)
        return page

    def _log_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Arena Log File")
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
                button.clicked.connect(lambda _checked, p=path: self._log_input.setText(str(p)))
                layout.addWidget(button)
        else:
            layout.addWidget(
                QLabel("No Player.log found yet — you can configure this later in Settings.")
            )

        browse_row = QHBoxLayout()
        browse_row.addWidget(self._log_input)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: browse_for_log(self, self._log_input))
        browse_row.addWidget(browse)
        layout.addLayout(browse_row)
        return page

    def _finish_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Ready to Sync")
        layout = QVBoxLayout(page)
        outro = QLabel(
            "Click <b>Finish</b> to save your settings and start syncing. "
            "The 17Lands tray app will run in the background and start on login."
        )
        outro.setWordWrap(True)
        layout.addWidget(outro)
        page.setFinalPage(True)
        return page

    def accept(self) -> None:
        token = self._token_input.text().strip()
        if validate_and_save(self, token, self._log_input.text().strip()):
            super().accept()


def run_setup_wizard(parent: QWidget | None = None) -> bool:
    return SetupWizard(parent).exec() == QDialog.DialogCode.Accepted


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None, focus_token: bool = False) -> None:
        super().__init__(parent)
        settings = ClientSettings.load()

        self.setWindowTitle(f"{APP_NAME} Settings")
        self.setWindowIcon(load_app_icon())
        self.setMinimumWidth(540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(16)
        intro = QLabel(
            "Your client token links uploads to your 17Lands account. "
            f"Find it at {link(ACCOUNT_URL, '17lands.com/account')}."
        )
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self._token_input = make_token_input(settings)
        form.addRow("Client Token", token_row(self._token_input))

        log_row = QHBoxLayout()
        self._log_input = QLineEdit(settings.log_file or "")
        self._log_input.setPlaceholderText("Auto-detect if empty")
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(lambda: browse_for_log(self, self._log_input))
        log_row.addWidget(self._log_input)
        log_row.addWidget(browse_button)
        form.addRow("Arena Log File", log_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if focus_token:
            self._token_input.setFocus()
            self._token_input.selectAll()

    def _save(self) -> None:
        if validate_and_save(
            self, self._token_input.text().strip(), self._log_input.text().strip()
        ):
            self.accept()
        else:
            self._token_input.setFocus()


# --- Status window ----------------------------------------------------------


class StatTile(QFrame):
    """A small card with a big value and a caption underneath."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setObjectName("tile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        self.value = QLabel("—")
        self.value.setObjectName("tileValue")
        self.caption = QLabel(caption)
        self.caption.setObjectName("muted")
        self.caption.setWordWrap(True)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)


class StatusWindow(QWidget):
    def __init__(self, controller: SeventeenLandsTray) -> None:
        super().__init__()
        self._controller = controller
        self._snapshot: ClientSnapshot | None = None
        self._applying_theme = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(440, 600)
        self.resize(480, 680)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 14)
        root.setSpacing(14)

        # Header: badge, name + account, start/stop.
        header = QHBoxLayout()
        header.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        self._account_label = QLabel()
        self._account_label.setObjectName("muted")
        titles.addWidget(title)
        titles.addWidget(self._account_label)
        header.addLayout(titles, stretch=1)
        self._toggle_button = QPushButton()
        self._toggle_button.clicked.connect(self._controller.toggle_follower)
        header.addWidget(self._toggle_button)
        root.addLayout(header)

        # Status banner.
        self._banner = QFrame()
        self._banner.setObjectName("banner")
        banner_layout = QHBoxLayout(self._banner)
        banner_layout.setContentsMargins(14, 12, 14, 12)
        banner_layout.setSpacing(12)
        self._banner_dot = QLabel()
        self._banner_dot.setFixedSize(16, 22)
        self._banner_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner_layout.addWidget(self._banner_dot, alignment=Qt.AlignmentFlag.AlignTop)
        banner_text = QVBoxLayout()
        banner_text.setSpacing(2)
        self._headline = QLabel()
        self._headline.setObjectName("headline")
        self._detail = QLabel()
        self._detail.setObjectName("detail")
        self._detail.setWordWrap(True)
        banner_text.addWidget(self._headline)
        banner_text.addWidget(self._detail)
        banner_layout.addLayout(banner_text, stretch=1)
        root.addWidget(self._banner)

        # Stat tiles.
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self._last_upload_tile = StatTile("Last Upload")
        self._session_tile = StatTile("Session Uploads")
        self._activity_tile = StatTile("Last Activity")
        for tile in (self._last_upload_tile, self._session_tile, self._activity_tile):
            tiles.addWidget(tile, stretch=1)
        root.addLayout(tiles)

        # Details card.
        details = QFrame()
        details.setObjectName("card")
        form = QFormLayout(details)
        form.setContentsMargins(14, 10, 14, 10)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(6)
        self._client_label = QLabel()
        self._token_label = QLabel()
        self._log_label = QLabel()
        for name, label in (
            ("Client", self._client_label),
            ("Token", self._token_label),
            ("Arena Log", self._log_label),
        ):
            caption = QLabel(name)
            caption.setObjectName("muted")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(caption, label)
        root.addWidget(details)

        # Recent activity.
        activity_header = QVBoxLayout()
        activity_header.setSpacing(2)
        activity_title = QLabel("Recent Activity")
        activity_title.setObjectName("section")
        activity_hint = QLabel("Uploads run in the background, even with this window closed.")
        activity_hint.setObjectName("muted")
        activity_hint.setWordWrap(True)
        activity_header.addWidget(activity_title)
        activity_header.addWidget(activity_hint)
        root.addLayout(activity_header)

        self._events = QTreeWidget()
        self._events.setObjectName("events")
        self._events.setColumnCount(2)
        self._events.setHeaderHidden(True)
        self._events.setRootIsDecorated(False)
        self._events.setIndentation(0)
        self._events.setUniformRowHeights(True)
        self._events.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self._events.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        events_header = self._events.header()
        events_header.setStretchLastSection(False)
        events_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        events_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self._events, stretch=1)

        # Actions.
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._icon_buttons: list[tuple[QPushButton, tuple[str, ...]]] = []
        for text, icons, slot in (
            ("Restart", ("view-refresh",), self._controller.restart_follower),
            ("Settings", ("configure", "preferences-system"), self._open_settings),
            ("Open Log", ("text-x-generic", "document-open"), self._controller.open_log),
            ("Update", ("system-software-update", "update-none"), self._controller.start_update),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            self._icon_buttons.append((button, icons))
            actions.addWidget(button, stretch=1)
        self._update_button = button
        root.addLayout(actions)

        # Footer.
        footer = QHBoxLayout()
        quit_button = QPushButton("Quit 17Lands")
        self._icon_buttons.append((quit_button, ("application-exit",)))
        quit_button.setFlat(True)
        quit_button.setToolTip(
            "Close 17Lands completely and stop uploading.\n"
            "Closing this window keeps 17Lands running in the system tray."
        )
        quit_button.clicked.connect(self._controller.confirm_quit)
        footer.addWidget(quit_button)
        footer.addStretch(1)
        self._credit = QLabel()
        self._credit.setOpenExternalLinks(True)
        footer.addWidget(self._credit)
        root.addLayout(footer)

        self._apply_theme()

    def set_update_state(self, latest: str | None, updating: bool) -> None:
        button = self._update_button
        button.setVisible(bool(latest) or updating)
        button.setEnabled(not updating)
        font = button.font()
        font.setBold(bool(latest) and not updating)
        button.setFont(font)
        if updating:
            button.setText("Updating…")
            button.setToolTip("Installing the latest version. 17Lands will restart shortly.")
        else:
            button.setText(f"Update to {latest}")
            button.setToolTip(f"Version {latest} is available (you have {__version__}).")

    def _refresh_icons(self) -> None:
        # Monochrome theme icons must match the palette (breeze vs breeze-dark).
        theme = QIcon.themeName()
        if theme.startswith("breeze"):
            QIcon.setThemeName("breeze-dark" if self._is_dark() else "breeze")
        for button, names in self._icon_buttons:
            button.setIcon(theme_icon(*names))
        if self._snapshot is not None:
            self._update_toggle(self._snapshot.process_running)

    def _update_toggle(self, running: bool) -> None:
        self._toggle_button.setText("Stop" if running else "Start")
        self._toggle_button.setIcon(
            theme_icon("media-playback-stop" if running else "media-playback-start")
        )
        self._toggle_button.setToolTip(
            "Stop uploading to 17Lands" if running else "Start uploading to 17Lands"
        )

    def _open_settings(self) -> None:
        self._controller.show_settings_dialog()

    # --- theming ---

    def _is_dark(self) -> bool:
        return QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _apply_theme(self) -> None:
        if self._applying_theme:
            return
        self._applying_theme = True
        try:
            palette = QApplication.palette()
            window = palette.color(QPalette.ColorRole.Window)
            text = palette.color(QPalette.ColorRole.WindowText)
            base = palette.color(QPalette.ColorRole.Base)
            border = mix(window, text, 0.14).name()
            muted = mix(text, window, 0.42).name()
            self._theme_key = self._palette_key()
            self.setStyleSheet(
                f"""
                QLabel#title {{ font-size: 18px; font-weight: 700; }}
                QLabel#muted {{ color: {muted}; }}
                QLabel#section {{ font-size: 14px; font-weight: 600; }}
                QLabel#headline {{ font-size: 16px; font-weight: 700; }}
                QLabel#tileValue {{ font-size: 18px; font-weight: 700; }}
                QFrame#tile, QFrame#card {{
                    background: {base.name()};
                    border: 1px solid {border};
                    border-radius: 10px;
                }}
                QFrame#tile QLabel, QFrame#card QLabel {{ border: none; background: transparent; }}
                QTreeWidget#events {{
                    background: {base.name()};
                    border: 1px solid {border};
                    border-radius: 10px;
                    padding: 4px;
                }}
                QTreeWidget#events::item {{ padding: 5px 4px; border: none; }}
                """
            )
            self._muted_color = QColor(muted)
            muted_color = QColor(muted)
            self._credit.setText(
                f"{link(SITE_URL, '17lands.com', muted_color, underline=False)}"
                f'<span style="color: {muted};"> · v{__version__} · </span>'
                f"{link(CREDIT_URL, 'Built by Greg.Tech', muted_color, underline=False)}"
            )
            self._refresh_icons()
            if self._snapshot is not None:
                self.update_snapshot(self._snapshot)
        finally:
            self._applying_theme = False

    @staticmethod
    def _palette_key() -> tuple[str, ...]:
        palette = QApplication.palette()
        roles = (QPalette.ColorRole.Window, QPalette.ColorRole.WindowText, QPalette.ColorRole.Base)
        return tuple(palette.color(role).name() for role in roles)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        ) and self._palette_key() != getattr(self, "_theme_key", None):
            # The desktop color scheme changed (e.g. light <-> dark).
            QTimer.singleShot(0, self._apply_theme)

    def closeEvent(self, event) -> None:  # noqa: N802
        # The X only hides the window; 17Lands keeps running in the tray.
        event.ignore()
        self.hide()
        self._controller.on_window_closed()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    # --- content ---

    def _style_banner(self, state: str) -> None:
        palette = QApplication.palette()
        window = palette.color(QPalette.ColorRole.Window)
        accent = QColor(STATE_ACCENTS.get(state, STATE_ACCENTS["idle"]))
        dark = self._is_dark()
        background = mix(window, accent, 0.20 if dark else 0.12)
        border = mix(window, accent, 0.45 if dark else 0.35)
        headline = accent.lighter(140) if dark else accent.darker(130)
        detail = mix(palette.color(QPalette.ColorRole.WindowText), headline, 0.35)
        self._banner.setStyleSheet(
            f"QFrame#banner {{ background: {background.name()}; "
            f"border: 1px solid {border.name()}; border-radius: 12px; }}"
            f"QFrame#banner QLabel {{ background: transparent; border: none; }}"
            f"QLabel#headline {{ color: {headline.name()}; }}"
            f"QLabel#detail {{ color: {detail.name()}; }}"
        )
        self._banner_dot.setPixmap(dot_icon(accent.name(), 12).pixmap(16, 16))

    def update_snapshot(self, snapshot: ClientSnapshot) -> None:
        self._snapshot = snapshot
        now = datetime.now()

        self._style_banner(snapshot.state)
        self._headline.setText(STATE_HEADLINES.get(snapshot.state, snapshot.state.title()))
        self._detail.setText(snapshot.state_detail)

        self._account_label.setText(
            f"Signed in to Arena as {snapshot.account}"
            if snapshot.account
            else "Arena account not detected yet"
        )

        running = snapshot.process_running
        self._update_toggle(running)

        self._last_upload_tile.value.setText(format_relative(snapshot.last_sync_at, now))
        self._session_tile.value.setText(str(snapshot.session_uploads))
        self._activity_tile.value.setText(format_relative(snapshot.last_event_at, now))

        settings = ClientSettings.load()
        if snapshot.external_process:
            client = "Running (started outside the tray)"
        elif running:
            client = "Running in the background"
        else:
            client = "Not running"
        self._client_label.setText(client)
        self._token_label.setText(settings.masked_token())
        log_path = settings.log_file or snapshot.arena_log
        self._log_label.setText(short_log_path(log_path))
        self._log_label.setToolTip(log_path or "")

        self._events.clear()
        if not snapshot.recent_events:
            item = QTreeWidgetItem(
                ["No activity yet. Launch MTG Arena and play or draft to see events here.", ""]
            )
            item.setForeground(0, self._muted_color)
            self._events.addTopLevelItem(item)
            return
        for event in snapshot.recent_events:
            item = QTreeWidgetItem([event.summary, format_event_time(event.timestamp, now)])
            item.setIcon(0, dot_icon(KIND_COLORS.get(event.kind, KIND_COLORS["info"])))
            item.setForeground(1, self._muted_color)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip(1, f"{event.timestamp:%Y-%m-%d %H:%M:%S}")
            self._events.addTopLevelItem(item)


# --- Tray controller --------------------------------------------------------


class SeventeenLandsTray(QSystemTrayIcon):
    def __init__(self, *, defer_start: bool = False) -> None:
        super().__init__()
        self._process = QProcess()
        # The client already writes its own log file; keep only a short tail of
        # stdout/stderr (for crash messages) instead of buffering it forever.
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.setStandardInputFile(QProcess.nullDevice())
        self._process.readyReadStandardOutput.connect(self._drain_output)
        self._process.finished.connect(self._on_process_finished)
        self._process.started.connect(self._on_process_started)
        self._process.errorOccurred.connect(self._on_process_error)
        self._output_tail: deque[str] = deque(maxlen=CLIENT_OUTPUT_LINES)

        # Reinstalls the client when a Python upgrade breaks it (once per breakage).
        self._repair = QProcess()
        self._repair.setStandardInputFile(QProcess.nullDevice())
        self._repair.finished.connect(self._on_repair_finished)
        self._repair.errorOccurred.connect(self._on_repair_error)
        self._repair_attempted = False

        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self.start_follower)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(STATUS_POLL_MS)

        self.quitting = False
        self._auto_restart = True
        self._expected_exit = False
        self._starting = False
        self._external_process = False
        self._restart_delay_ms = RESTART_DELAY_MS
        self._started_monotonic = 0.0
        self._session_start = datetime.now()
        self._snapshot = ClientSnapshot(state="starting", state_detail=STATE_DETAILS["starting"])
        self._icons = {name: make_17_icon(QColor(color)) for name, color in ICON_COLORS.items()}

        self.window = StatusWindow(self)
        self._menu = self._build_menu()
        # setContextMenu exports the menu over DBusMenu, so Plasma (and other
        # StatusNotifier hosts) place it correctly, including on Wayland.
        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)

        self.setToolTip(APP_NAME)
        self._apply_snapshot(self._snapshot, self._icons["starting"])
        self._ensure_tray_visible()
        self._setup_updates()
        if not defer_start:
            self.start_follower()

    # --- updates ---

    def _setup_updates(self) -> None:
        self.latest_version: str | None = None
        self._updating = False
        self._network = QNetworkAccessManager(self)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self.check_for_update)
        self._update_timer.start(UPDATE_CHECK_INTERVAL_MS)
        QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self.check_for_update)
        QTimer.singleShot(3_000, self._report_update_result)
        self._sync_update_ui()

    def _report_update_result(self) -> None:
        result = read_update_result()
        if result is None:
            return
        previous, ok = result
        if ok and previous != __version__:
            message = f"Updated from v{previous} to v{__version__}."
        elif ok:
            message = f"You're on the latest version (v{__version__}). The client was refreshed."
        else:
            message = f"The update didn't finish. Details are in {UPDATE_LOG}."
        icon = QSystemTrayIcon.MessageIcon
        self.showMessage(APP_NAME, message, icon.Information if ok else icon.Warning)

    def check_for_update(self) -> None:
        request = QNetworkRequest(QUrl(VERSION_URL))
        request.setTransferTimeout(15_000)
        reply = self._network.get(request)
        reply.finished.connect(lambda: self._on_version_reply(reply))

    def _on_version_reply(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            return  # offline or GitHub unreachable; try again later
        remote = parse_version(bytes(reply.readAll()).decode("utf-8", errors="replace"))
        if remote is None or remote <= version_tuple(__version__):
            return
        latest = f"v{format_version(remote)}"
        if latest != self.latest_version:
            self.latest_version = latest
            self.showMessage(
                APP_NAME,
                f"Version {latest} is available. Open the dashboard and click Update.",
                QSystemTrayIcon.MessageIcon.Information,
            )
        self._sync_update_ui()

    def _sync_update_ui(self) -> None:
        self.window.set_update_state(self.latest_version, self._updating)
        # Only offer updating when GitHub has a newer version.
        self._update_action.setVisible(bool(self.latest_version) and not self._updating)
        if self.latest_version:
            self._update_action.setText(f"Update to {self.latest_version}…")

    def start_update(self, *, confirm: bool = True) -> None:
        if self._updating:
            return
        if confirm:
            target = self.latest_version or "the latest version"
            answer = QMessageBox.question(
                self.window if self.window.isVisible() else None,
                f"Update {APP_NAME}",
                f"Download and install {target} from GitHub?\n\n"
                "This also updates the seventeenlands client. 17Lands closes "
                "while it updates and reopens in about a minute.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not launch_update():
            QMessageBox.warning(
                None,
                APP_NAME,
                f"Couldn't start the update. Run this in a terminal instead:\n\n"
                f"curl -fsSL {INSTALL_URL} | bash",
            )
            return
        self._updating = True
        self._sync_update_ui()
        self.showMessage(
            APP_NAME,
            "Updating… 17Lands will restart in about a minute.",
            QSystemTrayIcon.MessageIcon.Information,
        )

    def _ensure_tray_visible(self, attempts: int = 0) -> None:
        # At login the tray host may appear after we start; keep checking.
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.show()
            return
        delay = 2_000 if attempts < 30 else 15_000
        QTimer.singleShot(delay, lambda: self._ensure_tray_visible(attempts + 1))

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        menu.setTitle(APP_NAME)
        menu.addAction("Open Dashboard", self.show_status_window)
        self._start_action = menu.addAction("Start", self.start_follower)
        self._stop_action = menu.addAction("Stop", self.stop_follower)
        menu.addAction("Restart", self.restart_follower)
        menu.addSeparator()
        menu.addAction("Run Setup Wizard…", self.show_setup_wizard)
        menu.addAction("Settings…", lambda: self.show_settings_dialog())
        menu.addAction("Re-authenticate…", self.show_reauthenticate_dialog)
        menu.addSeparator()
        menu.addAction("View Log File", self.open_log)
        menu.addAction("Open 17lands.com", lambda: open_url(SITE_URL))
        self._update_action = menu.addAction("Update…", lambda: self.start_update())
        menu.addSeparator()
        menu.addAction("Quit 17Lands…", self.confirm_quit)
        return menu

    # --- user actions ---

    def show_setup_wizard(self) -> None:
        if run_setup_wizard(self.window):
            self.restart_follower()
            self.show_status_window()

    def show_settings_dialog(self, *, focus_token: bool = False) -> None:
        dialog = SettingsDialog(self.window, focus_token=focus_token)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.restart_follower()
            self.show_status_window()

    def show_reauthenticate_dialog(self) -> None:
        self.show_settings_dialog(focus_token=True)

    def handle_command(self, command: str) -> None:
        command = command.strip().lower()
        if command == "setup":
            self.show_setup_wizard()
        elif command in {"reauthenticate", "reauth"}:
            self.show_reauthenticate_dialog()
        elif command in {"settings", "authenticate"}:
            self.show_settings_dialog()
        elif command == "show":
            self.show_status_window()
        elif command == "update":
            self.start_update()
        elif command == "quit":
            self.quit_app()

    def toggle_follower(self) -> None:
        if self._snapshot.process_running:
            self.stop_follower()
        else:
            self.restart_follower()

    def show_status_window(self) -> None:
        self._refresh_status()
        self.window.update_snapshot(self._snapshot)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.show_settings_dialog()
        elif reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.window.isVisible() and self.window.isActiveWindow():
                self.window.hide()
            else:
                self.show_status_window()

    def open_log(self) -> None:
        if LOG_FILE.exists():
            open_url(QUrl.fromLocalFile(str(LOG_FILE)))
        else:
            self.showMessage(
                APP_NAME,
                f"Log file not found:\n{LOG_FILE}",
                QSystemTrayIcon.MessageIcon.Information,
            )

    # --- status ---

    def _own_process_running(self) -> bool:
        return self._process.state() != QProcess.ProcessState.NotRunning

    def _external_client_pids(self) -> list[int]:
        own = self._process.processId()
        return [pid for pid in find_pids(is_client_argv) if pid != own]

    def _apply_snapshot(self, snapshot: ClientSnapshot, icon: QIcon) -> None:
        self._snapshot = snapshot
        self.setIcon(icon)
        headline = STATE_HEADLINES.get(snapshot.state, snapshot.state.title())
        self.setToolTip(
            f"{APP_NAME}: {headline}\n{snapshot.state_detail}\n"
            f"Last upload: {format_relative(snapshot.last_sync_at)}"
        )
        running = snapshot.process_running
        self._start_action.setEnabled(not running)
        self._stop_action.setEnabled(running)
        if self.window.isVisible():
            self.window.update_snapshot(snapshot)

    def _refresh_status(self) -> None:
        own_running = self._own_process_running()
        external_running = not own_running and bool(self._external_client_pids())

        if self._external_process and not external_running and not own_running:
            # The external client we deferred to has gone away; take over.
            self._external_process = False
            if self._auto_restart and not self._restart_timer.isActive():
                self.start_follower()
                return

        snapshot = LogAnalyzer.build_snapshot(
            process_running=own_running or external_running,
            external_process=external_running,
            starting=self._starting and own_running,
            session_start=self._session_start,
        )
        if self._repairing() or (self._restart_timer.isActive() and not snapshot.process_running):
            return  # keep the "restarting in Ns" / "repairing" message visible
        icon = self._icons[STATE_ICONS.get(snapshot.state, "starting")]
        self._apply_snapshot(snapshot, icon)

    # --- process management ---

    def start_follower(self) -> None:
        self._restart_timer.stop()
        if self._own_process_running() or self._repairing():
            return

        if self._external_client_pids():
            self._external_process = True
            self._refresh_status()
            return
        self._external_process = False

        command = client_command()
        if not Path(command).exists():
            self._auto_restart = False
            snapshot = ClientSnapshot(
                state="error",
                state_detail=f"Can't find the client at {command}. Re-run the installer.",
            )
            self._apply_snapshot(snapshot, self._icons["stopped"])
            self.showMessage(
                APP_NAME,
                f"Could not find seventeenlands at {command}",
                QSystemTrayIcon.MessageIcon.Critical,
            )
            return

        # The client reads its token from ~/.mtga_follower.ini itself; never pass
        # it on the command line, where any local user could read it via ps.
        args: list[str] = []
        settings = ClientSettings.load()
        if settings.log_file:
            args.extend(["-l", settings.log_file])

        self._auto_restart = True
        self._starting = True
        self._output_tail.clear()
        self._process.setProgram(command)
        self._process.setArguments(args)
        self._process.start()
        self._refresh_status()

    def _drain_output(self) -> None:
        data = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._output_tail.extend(line for line in data.splitlines() if line.strip())

    def _stop_processes(self) -> None:
        """Stop our client and any external one, without changing auto-restart."""
        self._restart_timer.stop()
        self._starting = False
        if self._own_process_running():
            self._expected_exit = True
            self._process.terminate()
            if not self._process.waitForFinished(3000):
                self._process.kill()
                self._process.waitForFinished(1000)
            self._expected_exit = False
        terminate_pids(self._external_client_pids())
        self._external_process = False

    def stop_follower(self) -> None:
        self._auto_restart = False
        self._stop_processes()
        self._refresh_status()

    def restart_follower(self) -> None:
        self._stop_processes()
        self._restart_delay_ms = RESTART_DELAY_MS
        self.start_follower()

    def _on_process_started(self) -> None:
        self._starting = False
        self._started_monotonic = time.monotonic()
        self._session_start = datetime.now()
        self._refresh_status()

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return  # crashes are handled by finished()
        self._starting = False
        snapshot = ClientSnapshot(
            state="error",
            state_detail=f"Could not start {self._process.program()}: "
            f"{self._process.errorString()}",
        )
        self._apply_snapshot(snapshot, self._icons["error"])

    def _on_process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._drain_output()
        self._starting = False
        if self._expected_exit or not self._auto_restart:
            self._refresh_status()
            return

        if time.monotonic() - self._started_monotonic >= STABLE_RUN_SECONDS:
            self._restart_delay_ms = RESTART_DELAY_MS
            self._repair_attempted = False  # it ran fine; a later breakage gets a new repair
        broken = client_install_broken(list(self._output_tail))
        if broken and not self._repair_attempted:
            self._start_repair()
            return
        delay = self._restart_delay_ms
        self._restart_delay_ms = min(delay * 2, MAX_RESTART_DELAY_MS)

        detail = f"Client exited (code {exit_code}); restarting in {delay // 1000}s"
        last_line = self._output_tail[-1] if self._output_tail else ""
        if broken:
            detail += (
                "\nThe client install is broken and automatic repair failed. "
                "Re-run the installer (details in client-repair.log)."
            )
        elif last_line:
            detail += f"\n{last_line[:200]}"
        snapshot = ClientSnapshot(state="restarting", state_detail=detail)
        self._apply_snapshot(snapshot, self._icons["error"])
        self._restart_timer.start(delay)

    # --- client repair (after a Python upgrade) ---

    def _repairing(self) -> bool:
        return self._repair.state() != QProcess.ProcessState.NotRunning

    def _start_repair(self) -> None:
        self._repair_attempted = True
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = ClientSnapshot(
            state="repairing",
            state_detail="Python was upgraded, which broke the client install. "
            "Reinstalling it now; this takes a minute.",
        )
        self._apply_snapshot(snapshot, self._icons["syncing"])
        self._repair.start("/bin/bash", ["-c", repair_script(REPAIR_LOG)])

    def _on_repair_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._on_repair_finished(-1, QProcess.ExitStatus.CrashExit)

    def _on_repair_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if exit_code == 0:
            self.showMessage(
                APP_NAME,
                "Reinstalled the 17Lands client after a Python upgrade. Uploads are back on.",
                QSystemTrayIcon.MessageIcon.Information,
            )
        if not self._auto_restart or self.quitting:
            self._refresh_status()
            return
        self._restart_delay_ms = RESTART_DELAY_MS
        # On failure the client exits again; with the repair already attempted,
        # that shows the "re-run the installer" message and keeps retrying.
        self.start_follower()

    def shutdown(self) -> None:
        self._auto_restart = False
        self._status_timer.stop()
        if self._repairing():
            self._repair.kill()
            self._repair.waitForFinished(1000)
        self._stop_processes()

    def confirm_quit(self) -> None:
        box = QMessageBox(self.window if self.window.isVisible() else None)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(f"Quit {APP_NAME}")
        box.setText("Quit 17Lands and stop uploading?")
        box.setInformativeText(
            "Uploads stop until you open 17Lands again or log in next time.\n\n"
            "To keep uploading, close the window instead. 17Lands stays in "
            "your system tray."
        )
        quit_button = box.addButton("Quit 17Lands", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is quit_button:
            self.quit_app()

    def on_window_closed(self) -> None:
        config = _read_config(TRAY_SETTINGS_FILE)
        if config.has_section("ui") and config["ui"].getboolean("close_hint_shown", False):
            return
        self.showMessage(
            APP_NAME,
            "17Lands is still running in your system tray and keeps uploading. "
            "Use Quit 17Lands to close it completely.",
            QSystemTrayIcon.MessageIcon.Information,
        )
        if not config.has_section("ui"):
            config["ui"] = {}
        config["ui"]["close_hint_shown"] = "true"
        with contextlib.suppress(OSError):
            _write_config(TRAY_SETTINGS_FILE, config, private=False)

    def quit_app(self) -> None:
        self.quitting = True
        self.shutdown()
        self.window.hide()
        self.hide()
        # exit() (unlike quit()) also ends nested loops such as an open setup
        # wizard, including before app.exec() has started.
        QApplication.exit(0)


# --- Command line -----------------------------------------------------------


def quit_running_instance() -> int:
    tray_pids = find_pids(is_tray_argv)
    client_pids = find_pids(is_client_argv)
    if not tray_pids and not client_pids:
        print("17Lands tray is not running.")
        return 1

    if send_instance_command("quit"):
        tray_pids = wait_for_exit(tray_pids, timeout=8.0)
    terminate_pids(tray_pids)
    terminate_pids([pid for pid in client_pids if pid_alive(pid)])
    print("Stopped 17Lands tray.")
    return 0


def detach_from_terminal() -> None:
    """Re-launch detached so closing the terminal does not stop the tray app."""
    if os.environ.get(DETACH_ENV) == "1" or not sys.stdin.isatty():
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_ID,
        description="System tray app that keeps the 17Lands MTGA log client running.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--show", action="store_true", help="open the status window")
    actions.add_argument("--settings", "--authenticate", action="store_true", help="open settings")
    actions.add_argument(
        "--reauthenticate",
        "--reauth",
        dest="reauth",
        action="store_true",
        help="open settings with the token field focused",
    )
    actions.add_argument("--setup", action="store_true", help="run the setup wizard")
    actions.add_argument(
        "--update", action="store_true", help="update the tray app and client from GitHub"
    )
    actions.add_argument(
        "--setup-only",
        action="store_true",
        help="run the setup wizard and exit without starting the tray",
    )
    actions.add_argument("--quit", action="store_true", help="stop the tray app and client")
    parser.add_argument("--no-detach", action="store_true", help="stay attached to the terminal")
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="login autostart: exit quietly if the tray is already running",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def requested_command(args: argparse.Namespace) -> str | None:
    if args.setup:
        return "setup"
    if args.reauth:
        return "reauthenticate"
    if args.settings:
        return "settings"
    if args.show:
        return "show"
    if args.update:
        return "update"
    return None


def ensure_icon_theme() -> None:
    """PyQt6 from pip has no desktop integration: point it at the system icons."""
    if QIcon.themeName():
        return
    data_dirs = [os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))]
    data_dirs += os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    icon_dirs = [str(Path(d) / "icons") for d in data_dirs if (Path(d) / "icons").is_dir()]
    QIcon.setThemeSearchPaths([*QIcon.themeSearchPaths(), *icon_dirs])
    for name in ("breeze", "Adwaita"):
        if any((Path(d) / name).is_dir() for d in icon_dirs):
            QIcon.setThemeName(name)
            break
    QIcon.setFallbackThemeName("hicolor")


def create_app() -> QApplication:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(APP_NAME)
    QApplication.setApplicationVersion(__version__)
    QApplication.setDesktopFileName(APP_ID)
    QApplication.setQuitOnLastWindowClosed(False)
    app = QApplication(sys.argv[:1])
    ensure_icon_theme()
    app.setWindowIcon(load_app_icon())
    return app


def install_signal_handlers(app: QApplication, on_signal: Callable[[], None]) -> None:
    """Handle SIGINT/SIGTERM promptly even while Qt's event loop is idle."""
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    signal.set_wakeup_fd(writer.fileno())
    notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read, app)

    def drain() -> None:
        with contextlib.suppress(OSError):
            reader.recv(64)

    notifier.activated.connect(drain)
    app._signal_sockets = (reader, writer, notifier)  # keep alive

    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGINT, lambda *_: on_signal())
    signal.signal(signal.SIGTERM, lambda *_: on_signal())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.quit:
        return quit_running_instance()

    command = requested_command(args)

    if args.setup_only:
        app = create_app()  # noqa: F841 - must stay referenced while the wizard runs
        return 0 if run_setup_wizard() else 1

    # Already running: forward the request. A plain launch (e.g. from the app
    # menu) opens the status window; a login autostart stays quiet.
    forward = command or ("ping" if args.autostart else "show")
    if send_instance_command(forward):
        return 0

    if not args.no_detach:
        detach_from_terminal()

    app = create_app()

    tray: SeventeenLandsTray | None = None

    def on_command(cmd: str) -> None:
        if tray is not None:
            tray.handle_command(cmd)

    server = InstanceServer(on_command)
    if not server.listen():
        send_instance_command(forward)
        return 0

    need_setup = args.setup or not ClientSettings.is_configured()
    tray = SeventeenLandsTray(defer_start=need_setup)
    app.aboutToQuit.connect(tray.shutdown)
    app.aboutToQuit.connect(server.close)
    install_signal_handlers(app, tray.quit_app)

    if need_setup:
        completed = run_setup_wizard(tray.window)
        if tray.quitting:
            server.close()
            return 0
        if not completed and not ClientSettings.is_configured():
            print("Setup cancelled.")
            tray.shutdown()
            server.close()
            return 1
        tray.start_follower()
    elif command == "reauthenticate":
        QTimer.singleShot(0, tray.show_reauthenticate_dialog)
    elif command == "settings":
        QTimer.singleShot(0, tray.show_settings_dialog)
    elif command == "show":
        QTimer.singleShot(0, tray.show_status_window)
    elif command == "update":
        QTimer.singleShot(0, tray.start_update)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
