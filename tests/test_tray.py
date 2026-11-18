"""Tests for the non-GUI logic in seventeenlands_tray.

Run with:  QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import seventeenlands_tray as tray  # noqa: E402

NOW = datetime(2026, 11, 2, 12, 0, 0)


def line(when: datetime, message: str, level: str = "INFO") -> str:
    return f"{when:%Y%m%d %H%M%S}.000,{level},17Lands,{message}"


def entries(*lines: str) -> list[tray.LogEntry]:
    return tray.LogAnalyzer.parse_lines(list(lines))


def snapshot(log_lines, *, running=True, session_start=None, starting=False):
    return tray.LogAnalyzer.build_snapshot(
        process_running=running,
        external_process=False,
        starting=starting,
        session_start=session_start,
        entries=entries(*log_lines),
        now=NOW,
    )


class SummarizeTests(unittest.TestCase):
    def test_known_messages(self):
        summarize = tray.LogAnalyzer.summarize_message
        self.assertEqual(
            summarize("Updating user info: {'screen_name': 'Greg#12345'}"),
            "Logged in as Greg#12345",
        )
        self.assertEqual(
            summarize("Human draft pick (combined): {'pack_number': 2, 'pick_number': 7}"),
            "Draft pick recorded (pack 2, pick 7)",
        )
        self.assertEqual(
            summarize(
                "Draft pick: {'event_name': 'QuickDraft', 'pack_number': 1, 'pick_number': 3}"
            ),
            "Draft pick recorded (pack 1, pick 3)",
        )
        self.assertEqual(
            summarize("Added pending match result {'won_match': True}"),
            "Match win queued for upload",
        )
        self.assertIsNone(summarize("Some unrelated debug line"))

    def test_parse_lines_skips_garbage(self):
        parsed = entries("not a log line", line(NOW, "Exiting"), "Traceback (most recent call)")
        self.assertEqual([e.message for e in parsed], ["Exiting"])


class SnapshotTests(unittest.TestCase):
    def test_stopped(self):
        self.assertEqual(snapshot([], running=False).state, "stopped")

    def test_old_errors_do_not_stick(self):
        old_error = line(NOW - timedelta(hours=3), "boom", level="ERROR")
        recent = line(NOW - timedelta(seconds=30), "Updated ongoing events")
        self.assertEqual(snapshot([old_error, recent]).state, "watching")

    def test_recent_error_in_session(self):
        error = line(NOW - timedelta(seconds=30), "boom", level="ERROR")
        self.assertEqual(snapshot([error]).state, "error")

    def test_error_before_session_start_ignored(self):
        error = line(NOW - timedelta(seconds=90), "boom", level="ERROR")
        snap = snapshot([error], session_start=NOW - timedelta(seconds=60))
        self.assertNotEqual(snap.state, "error")

    def test_waiting_for_log_is_order_aware(self):
        following = line(NOW - timedelta(minutes=2), "Following along /x/Player.log")
        missing = line(NOW - timedelta(minutes=1), "Found no files to parse. Try -l")
        self.assertEqual(snapshot([following, missing]).state, "waiting")
        self.assertNotEqual(snapshot([missing, following]).state, "waiting")

    def test_syncing_and_up_to_date(self):
        upload = line(NOW - timedelta(seconds=5), "Submitting queued game result")
        self.assertEqual(snapshot([upload]).state, "syncing")
        upload = line(NOW - timedelta(minutes=2), "Submitting queued game result")
        self.assertEqual(snapshot([upload]).state, "up_to_date")

    def test_idle(self):
        self.assertEqual(snapshot([line(NOW - timedelta(hours=1), "Exiting")]).state, "idle")

    def test_session_uploads_counted_from_session_start(self):
        logs = [
            line(NOW - timedelta(hours=2), "Submitting queued game result"),
            line(NOW - timedelta(minutes=10), "Submitting queued game result"),
            line(NOW - timedelta(minutes=5), "Submitting queued game result"),
        ]
        snap = snapshot(logs, session_start=NOW - timedelta(hours=1))
        self.assertEqual(snap.session_uploads, 2)
        self.assertEqual(snap.last_sync_at, NOW - timedelta(minutes=5))

    def test_account_and_recent_events(self):
        logs = [
            line(NOW - timedelta(minutes=3), "Following along /games/Player.log"),
            line(NOW - timedelta(minutes=2), "Updating user info: {'screen_name': 'Greg'}"),
        ]
        snap = snapshot(logs)
        self.assertEqual(snap.account, "Greg")
        self.assertEqual(snap.arena_log, "/games/Player.log")
        self.assertEqual(snap.recent_events[0].summary, "Logged in as Greg")


class CatchUpAndKindTests(unittest.TestCase):
    def test_catching_up_right_after_start(self):
        upload = line(NOW - timedelta(seconds=3), "Submitting queued game result")
        started = snapshot([upload], session_start=NOW - timedelta(seconds=30))
        self.assertEqual(started.state, "catching_up")
        later = snapshot([upload], session_start=NOW - timedelta(minutes=30))
        self.assertEqual(later.state, "syncing")

    def test_event_kinds(self):
        classify = tray.LogAnalyzer.classify_message
        self.assertEqual(classify("Added pending match result {'won_match': True}", "INFO"), "win")
        self.assertEqual(
            classify("Added pending match result {'won_match': False}", "INFO"), "loss"
        )
        self.assertEqual(classify("Draft pick: {'pack_number': 1}", "INFO"), "draft")
        self.assertEqual(classify("Submitting queued game result", "INFO"), "upload")
        self.assertEqual(classify("boom", "ERROR"), "error")
        self.assertEqual(classify("Updated ongoing events", "INFO"), "info")

    def test_every_state_has_copy(self):
        for state in tray.STATE_ACCENTS:
            self.assertIn(state, tray.STATE_HEADLINES)


class LogCacheTests(unittest.TestCase):
    def test_reads_tail_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seventeenlands.log"
            path.write_text(line(NOW, "Exiting") + "\n")
            self.assertEqual(len(tray.LogAnalyzer.read_entries(path)), 1)
            with mock.patch.object(tray.LogAnalyzer, "read_tail") as read_tail:
                tray.LogAnalyzer.read_entries(path)
                read_tail.assert_not_called()
            with path.open("a") as handle:
                handle.write(line(NOW, "Updated ongoing events") + "\n")
            self.assertEqual(len(tray.LogAnalyzer.read_entries(path)), 2)
            self.assertEqual(tray.LogAnalyzer.read_entries(Path(tmp) / "missing"), [])


class ProcessMatchTests(unittest.TestCase):
    def test_client_argv(self):
        self.assertTrue(
            tray.is_client_argv(["/usr/bin/python3", "/home/u/.local/bin/seventeenlands"])
        )
        self.assertTrue(
            tray.is_client_argv(
                ["/usr/bin/python3.14", "/x/seventeenlands", "-l", "/a b/Player.log"]
            )
        )
        self.assertTrue(
            tray.is_client_argv(["/x/venvs/seventeenlands/bin/python", "/x/bin/seventeenlands"])
        )
        self.assertFalse(tray.is_client_argv(["vim", "seventeenlands"]))
        self.assertFalse(tray.is_client_argv(["/usr/bin/python3", "/x/seventeenlands-tray"]))

    def test_tray_argv(self):
        self.assertTrue(tray.is_tray_argv(["python3", "/home/u/.local/bin/seventeenlands-tray"]))
        self.assertTrue(
            tray.is_tray_argv(["/usr/bin/python3", "./seventeenlands_tray.py", "--quit"])
        )
        self.assertFalse(tray.is_tray_argv(["journalctl", "--user", "-u", "seventeenlands-tray"]))
        self.assertFalse(
            tray.is_tray_argv(["systemctl", "--user", "stop", "seventeenlands-tray.service"])
        )

    def test_find_pids_sees_only_matching_processes(self):
        self.assertEqual(tray.find_pids(lambda argv: False), [])
        own = os.getpid()
        self.assertNotIn(own, tray.find_pids(lambda argv: True))


class SettingsTests(unittest.TestCase):
    def test_token_file_is_private_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".mtga_follower.ini"
            config.write_text("[client]\ntoken = old\nother = keep\n")
            config.chmod(0o644)
            tray_settings = Path(tmp) / "tray" / "settings.ini"
            with (
                mock.patch.object(tray, "CONFIG_FILE", config),
                mock.patch.object(tray, "TRAY_SETTINGS_FILE", tray_settings),
            ):
                token = "0123456789abcdef0123456789abcdef"
                tray.ClientSettings().configure(token, "/tmp/Player.log")
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
                self.assertIn("other = keep", config.read_text())
                loaded = tray.ClientSettings.load()
                self.assertEqual(loaded.token, token)
                self.assertEqual(loaded.log_file, "/tmp/Player.log")
                self.assertEqual(loaded.masked_token(), "0123...cdef")

                tray.ClientSettings().save_log_file(None)
                self.assertIsNone(tray.ClientSettings.load().log_file)

    def test_invalid_token_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".mtga_follower.ini"
            config.write_text("[client]\ntoken = not-a-token\n")
            with (
                mock.patch.object(tray, "CONFIG_FILE", config),
                mock.patch.object(tray, "TRAY_SETTINGS_FILE", Path(tmp) / "none.ini"),
            ):
                self.assertFalse(tray.ClientSettings.is_configured())


class FormattingTests(unittest.TestCase):
    def test_format_relative(self):
        self.assertEqual(tray.format_relative(None), "Never")
        self.assertEqual(tray.format_relative(NOW - timedelta(seconds=10), NOW), "Just now")
        self.assertEqual(tray.format_relative(NOW - timedelta(minutes=5), NOW), "5 min ago")
        self.assertEqual(tray.format_relative(NOW - timedelta(hours=3), NOW), "3 hr ago")
        self.assertEqual(tray.format_relative(NOW - timedelta(days=2), NOW), "2 days ago")

    def test_format_event_time(self):
        self.assertEqual(tray.format_event_time(NOW.replace(hour=9, minute=5), NOW), "09:05")
        self.assertEqual(tray.format_event_time(NOW - timedelta(days=1), NOW), "Yesterday 12:00")
        self.assertEqual(tray.format_event_time(datetime(2026, 10, 3, 8, 30), NOW), "Oct 3 08:30")

    def test_short_log_path(self):
        self.assertEqual(
            tray.short_log_path("/home/u/.local/share/Steam/x/Player.log"), "Player.log (Steam)"
        )
        self.assertEqual(tray.short_log_path(None), "Not detected yet")


class ArgsTests(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(tray.requested_command(tray.parse_args(["--reauth"])), "reauthenticate")
        self.assertEqual(tray.requested_command(tray.parse_args(["--authenticate"])), "settings")
        self.assertIsNone(tray.requested_command(tray.parse_args(["--autostart"])))
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            tray.parse_args(["--setup", "--quit"])


class UpdateTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(tray.parse_version('x = 1\n__version__ = "1.10.2"\n'), (1, 10, 2))
        self.assertEqual(tray.parse_version("__version__ = '2.0'"), (2, 0))
        self.assertIsNone(tray.parse_version("no version here"))
        self.assertIsNone(tray.parse_version('__version__ = "1.2.beta"'))
        self.assertGreater((1, 10, 0), tray.version_tuple("1.9.9"))
        self.assertEqual(
            tray.parse_version(Path(tray.__file__).read_text()),
            tray.version_tuple(tray.__version__),
        )

    def test_update_script_uses_github_by_default(self):
        with mock.patch.dict(os.environ, {"SEVENTEENLANDS_TRAY_INSTALLER": ""}):
            script = tray.update_script(Path("/tmp/a b/update.log"))
        self.assertIn(f"curl -fsSL {tray.INSTALL_URL} | bash", script)
        self.assertIn("'/tmp/a b/update.log'", script)
        self.assertIn("systemctl --user start seventeenlands-tray.service", script)
        self.assertIn("set -o pipefail", script)

    def test_update_script_runs_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / "install.sh"
            log = Path(tmp) / "update.log"
            stub_bin = Path(tmp) / "bin"
            stub_bin.mkdir()
            (stub_bin / "systemctl").write_text("#!/bin/sh\nexit 0\n")
            (stub_bin / "systemctl").chmod(0o755)
            env = {**os.environ, "PATH": f"{stub_bin}:/usr/bin:/bin"}
            for code in (0, 3):
                installer.write_text(f"echo installing\nexit {code}\n")
                with mock.patch.dict(os.environ, {"SEVENTEENLANDS_TRAY_INSTALLER": str(installer)}):
                    script = tray.update_script(log)
                result = subprocess.run(["bash", "-c", script], env=env, check=False)
                self.assertEqual(result.returncode, code)
                self.assertIn("installing", log.read_text())
                self.assertTrue(log.read_text().strip().endswith(f"exit code {code}"))

    def test_update_marker_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker, log = Path(tmp) / "pending", Path(tmp) / "update.log"
            with (
                mock.patch.object(tray, "UPDATE_MARKER", marker),
                mock.patch.object(tray, "UPDATE_LOG", log),
            ):
                self.assertIsNone(tray.read_update_result())
                marker.write_text("1.1.0")
                log.write_text("stuff\n== Finished with exit code 0\n")
                self.assertEqual(tray.read_update_result(), ("1.1.0", True))
                self.assertIsNone(tray.read_update_result())  # reported once
                marker.write_text("1.1.0")
                log.write_text("== Finished with exit code 1\n")
                self.assertEqual(tray.read_update_result(), ("1.1.0", False))


class RepairTests(unittest.TestCase):
    def test_detects_broken_install(self):
        traceback = [
            "Traceback (most recent call last):",
            '  File "/home/u/.local/bin/seventeenlands", line 3, in <module>',
            "ModuleNotFoundError: No module named 'seventeenlands'",
        ]
        self.assertTrue(tray.client_install_broken(traceback))
        self.assertTrue(tray.client_install_broken(["ImportError: cannot import name 'x'"]))
        self.assertFalse(tray.client_install_broken(["requests.exceptions.ConnectionError"]))
        self.assertFalse(tray.client_install_broken([]))

    def run_repair(self, *, pip_rc: int, pipx: bool, pipx_link: bool = False):
        with tempfile.TemporaryDirectory() as tmp:
            home, stub_bin = Path(tmp) / "home", Path(tmp) / "bin"
            calls, log = Path(tmp) / "calls", Path(tmp) / "repair.log"
            (home / ".local/bin").mkdir(parents=True)
            stub_bin.mkdir()
            stubs = {"python3": pip_rc}
            if pipx:
                stubs["pipx"] = 0
            for name, code in stubs.items():
                stub = stub_bin / name
                stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit {code}\n')
                stub.chmod(0o755)
            client = home / ".local/bin/seventeenlands"
            if pipx_link:
                venv_bin = home / ".local/share/pipx/venvs/seventeenlands/bin"
                venv_bin.mkdir(parents=True)
                (venv_bin / "seventeenlands").write_text("")
                client.symlink_to(venv_bin / "seventeenlands")
            env = {**os.environ, "HOME": str(home), "PATH": f"{stub_bin}:/usr/bin:/bin"}
            result = subprocess.run(["bash", "-c", tray.repair_script(log)], env=env, check=False)
            ran = calls.read_text().splitlines() if calls.exists() else []
            return result.returncode, ran, log.read_text()

    def test_reinstalls_with_pip_user(self):
        rc, ran, log = self.run_repair(pip_rc=0, pipx=True)
        self.assertEqual(rc, 0)
        self.assertIn("python3 -m pip install --user", ran[-1])
        self.assertNotIn("pipx", " ".join(ran[1:]))
        self.assertTrue(log.strip().endswith("exit code 0"))

    def test_falls_back_to_pipx_when_pip_refuses(self):
        rc, ran, _ = self.run_repair(pip_rc=1, pipx=True)
        self.assertEqual(rc, 0)
        self.assertEqual(ran[-1], "pipx install --force seventeenlands")

    def test_reinstalls_existing_pipx_venv(self):
        rc, ran, _ = self.run_repair(pip_rc=0, pipx=True, pipx_link=True)
        self.assertEqual(rc, 0)
        self.assertEqual(ran[-1], "pipx reinstall seventeenlands")

    def test_reports_failure(self):
        rc, _, log = self.run_repair(pip_rc=1, pipx=False)
        self.assertEqual(rc, 1)
        self.assertTrue(log.strip().endswith("exit code 1"))


class CloseHintTests(unittest.TestCase):
    def test_close_hint_shows_once(self):
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            settings_file = Path(tmp) / "settings.ini"
            with (
                mock.patch.object(tray, "TRAY_SETTINGS_FILE", settings_file),
                mock.patch.object(tray, "CONFIG_FILE", Path(tmp) / "none.ini"),
                mock.patch.object(tray.SeventeenLandsTray, "showMessage") as show,
            ):
                controller = tray.SeventeenLandsTray(defer_start=True)
                controller.window.close()
                controller.window.close()
                self.assertEqual(show.call_count, 1)
                self.assertIn("close_hint_shown = true", settings_file.read_text())
                self.assertFalse(controller.window.isVisible())

                # Update is offered only once GitHub reports a newer version.
                self.assertTrue(controller.window._update_button.isHidden())
                self.assertFalse(controller._update_action.isVisible())
                controller.latest_version = "v9.9.9"
                controller._sync_update_ui()
                self.assertFalse(controller.window._update_button.isHidden())
                self.assertTrue(controller._update_action.isVisible())
                self.assertEqual(controller._update_action.text(), "Update to v9.9.9…")
                controller.shutdown()
        del app


class InstanceServerTests(unittest.TestCase):
    def test_commands_are_delivered_over_unix_socket(self):
        from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer

        app = QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tray, "SOCKET_PATH", Path(tmp) / "test.sock"):
                received: list[str] = []
                server = tray.InstanceServer(received.append)
                self.assertTrue(server.listen())
                self.assertFalse(tray.InstanceServer(lambda _c: None).listen())
                self.assertTrue(tray.send_instance_command("show"))

                loop = QEventLoop()
                deadline = QTimer()
                deadline.setSingleShot(True)
                deadline.timeout.connect(loop.quit)
                deadline.start(2000)
                poll = QTimer()
                poll.timeout.connect(lambda: "show" in received and loop.quit())
                poll.start(10)
                loop.exec()
                server.close()

                self.assertIn("show", received)
                self.assertFalse(tray.send_instance_command("show"))
        del app


if __name__ == "__main__":
    unittest.main()
