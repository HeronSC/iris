# File: core/tests/test_host.py

"""The headless host (11): background work that runs with no window open."""

from __future__ import annotations

import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from core.host.health import HOST_SERVICE, health_report
from core.host.service import IrisHost, probe_host
from core.storage.backups import BackupService
from core.storage.sqlite_database import SQLiteDatabase
from core.watchers.models import WatcherDefinition
from core.watchers.notify import LogNotifier
from core.watchers.service import WatcherService


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_config(root: Path) -> Path:
    (root / "Data" / "Memory").mkdir(parents=True)
    config = root / "config.json"
    config.write_text(
        json.dumps(
            {
                "assistant_name": "Iris",
                "memory_path": str(root / "Data" / "Memory"),
                "session_path": str(root / "Data" / "Sessions"),
                "audit_path": str(root / "Data" / "Audit"),
                "model": "qwen3:8b",
                "llm_server": "http://127.0.0.1:1",
                "llm_timeout_seconds": 1,
                "notifications": {"enabled": True},
                "document_search": {"catalog_path": str(root / "Data" / "Index" / "documents.db")},
            }
        ),
        encoding="utf-8",
    )
    return config


class HostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = _write_config(self.root)
        self.port = _free_port()
        self.host = IrisHost(self.config, http_port=self.port, heartbeat_seconds=1.0)

    def tearDown(self) -> None:
        self.host.stop()
        self.tempdir.cleanup()

    def _wait_for_health(self) -> dict[str, Any]:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            payload = probe_host(port=self.port)
            if payload is not None:
                return payload
            time.sleep(0.1)
        self.fail("the host never answered /health")

    def test_the_host_runs_watchers_schedules_and_http_with_no_window(self) -> None:
        self.host.start()
        payload = self._wait_for_health()

        self.assertEqual(payload["host"], HOST_SERVICE)
        self.assertTrue(self.host.watchers.running)
        self.assertTrue(self.host.schedules.running)
        self.assertEqual(sorted(payload["schedules"]["last_runs"]), ["Back up Data", "Re-appraise hypotheses", "Trim audit and traces"])
        self.assertEqual(payload["databases"]["knowledge"], "ok")
        self.assertIn("Ollama did not answer", " ".join(payload["broken"]))

    def test_stopping_takes_the_http_surface_down_with_it(self) -> None:
        self.host.start()
        self._wait_for_health()
        self.host.stop()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and probe_host(port=self.port) is not None:
            time.sleep(0.1)
        self.assertIsNone(probe_host(port=self.port))
        self.assertFalse(self.host.watchers.running)

    def test_a_watcher_added_by_another_process_is_picked_up(self) -> None:
        """Definitions are data on disk; whoever wrote them, the host runs them."""
        other = WatcherService(
            self.host.watchers.definitions_path,
            self.host.watchers.state_path,
            {"log": LogNotifier()},
        )
        other.add(WatcherDefinition(kind="path_missing", params={"path": str(self.root / "nope")}, channels=("log",), id="w1"))

        self.assertTrue(self.host.watchers.reload_if_changed())
        self.assertEqual([item.id for item in self.host.watchers.definitions()], ["w1"])
        self.assertFalse(self.host.watchers.reload_if_changed())

    def test_probe_returns_nothing_when_nobody_listens(self) -> None:
        self.assertIsNone(probe_host(port=_free_port()))


class HealthReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_damaged_database_makes_the_report_degraded(self) -> None:
        database = SQLiteDatabase(self.root / "k.db")
        with database.connect() as conn:
            conn.execute("CREATE TABLE t(x)")
            conn.executemany("INSERT INTO t VALUES (?)", [(index,) for index in range(200)])
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with database.db_path.open("r+b") as handle:
            handle.seek(4096 * 2 - 1024)
            handle.write(b"\xff" * 1024)

        class Service:
            config = {"assistant_name": "Iris"}
            backups = BackupService(self.root / "B", databases={"knowledge": SQLiteDatabase(database.db_path)})

        report = health_report(Service(), host="test")
        self.assertEqual(report["status"], "degraded")
        self.assertIn("knowledge.db failed quick_check and is read-only", report["broken"])

    def test_nothing_attached_is_still_a_report(self) -> None:
        class Bare:
            config = {"assistant_name": "Iris"}

        report = health_report(Bare(), host="test")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["broken"], [])


class DesktopAsClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = _write_config(self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_window_does_not_start_background_work_the_service_already_runs(self) -> None:
        #! @allow-local-import
        from core.application.service import IrisApplication

        answer = {"host": HOST_SERVICE, "watchers": {"defined": 2}, "schedules": {"defined": 2}}
        with patch("core.application.service.probe_host", return_value=answer):
            app = IrisApplication(self.config)
            app.initialize()
        try:
            self.assertFalse(app.watchers.running)
            self.assertFalse(app.schedules.running)
            self.assertTrue(any("client of it" in message.text for message in app.startup_messages))
        finally:
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
