# File: core/tests/test_storage_durability.py

"""SQLite durability and backups (11): WAL, quick_check, online copies, a tested restore."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.knowledge import KnowledgeGraph, MemoryKind, MemoryRecord
from core.knowledge.schema import SCHEMA_VERSION, ensure_schema, stored_version
from core.scheduler.jobs import DATABASE_BACKUP, build_jobs
from core.storage.backups import BackupService
from core.storage.sqlite_database import DatabaseDamaged, SQLiteDatabase


def _fill(database: SQLiteDatabase, rows: int = 500) -> None:
    with database.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t(x INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(index,) for index in range(rows)])
        conn.commit()


def _count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        connection.close()


def _damage(path: Path) -> None:
    """Smash the tail of page 2, where a b-tree page keeps its cells whatever its shape."""
    with path.open("r+b") as handle:
        handle.seek(4096 * 2 - 1024)
        handle.write(b"\xff" * 1024)


class DurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = SQLiteDatabase(self.root / "k.db")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_connections_run_in_wal_mode(self) -> None:
        _fill(self.database)
        self.assertEqual(self.database.journal_mode(), "wal")

    def test_a_fresh_database_passes(self) -> None:
        self.assertTrue(self.database.verify().ok)
        self.assertFalse(self.database.damaged)

    def test_a_damaged_database_is_read_only_and_says_so(self) -> None:
        """Refuse to write to a database that fails quick_check; do not refuse to read it."""
        _fill(self.database)
        with self.database.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _damage(self.database.db_path)

        reopened = SQLiteDatabase(self.database.db_path)
        self.assertFalse(reopened.verify().ok)
        self.assertTrue(reopened.damaged)
        with self.assertRaises(sqlite3.OperationalError):
            with reopened.connect() as conn:
                conn.execute("INSERT INTO t VALUES (1)")
        with reopened.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 1)
        with self.assertRaises(DatabaseDamaged):
            reopened.require_healthy()

    def test_the_report_is_bounded(self) -> None:
        _fill(self.database, rows=2000)
        with self.database.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _damage(self.database.db_path)
        report = SQLiteDatabase(self.database.db_path).verify()
        self.assertLessEqual(len(report.messages), 8)

    def test_an_online_copy_is_complete_and_self_contained(self) -> None:
        _fill(self.database, rows=300)
        copy = self.database.backup_to(self.root / "copies" / "k.db")

        self.assertEqual(_count(copy), 300)
        self.assertTrue(SQLiteDatabase(copy).verify().ok)
        self.assertFalse(copy.with_name("k.db-wal").exists())
        connection = sqlite3.connect(copy)
        self.assertEqual(str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "delete")
        connection.close()


class BackupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = SQLiteDatabase(self.root / "Memory" / "k.db")
        _fill(self.database, rows=100)
        self.config_folder = self.root / "Configuration"
        self.config_folder.mkdir()
        (self.config_folder / "watchers.json").write_text("[]", encoding="utf-8")
        self.now = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)
        self.service = BackupService(
            self.root / "Backups",
            databases={"knowledge": self.database},
            folders={"Configuration": self.config_folder},
            keep=2,
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_run_copies_databases_and_folders(self) -> None:
        report = self.service.run()

        self.assertTrue(report.ok, report.summary)
        self.assertEqual(_count(report.databases["knowledge"]), 100)
        self.assertTrue((report.folders["Configuration"] / "watchers.json").exists())
        self.assertIn("1 database(s) and 1 folder(s)", report.summary)

    def test_only_the_newest_runs_are_kept(self) -> None:
        for _ in range(4):
            self.service.run()
            self.now += timedelta(hours=1)
        self.assertEqual(len(self.service.runs()), 2)

    def test_a_restore_brings_the_data_back_and_keeps_what_it_replaced(self) -> None:
        """The checklist asks for a tested restore, not a backup that is assumed to work."""
        self.service.run()
        with self.database.connect() as conn:
            conn.execute("DELETE FROM t")
            conn.commit()
        self.assertEqual(_count(self.database.db_path), 0)
        (self.config_folder / "watchers.json").write_text("[{}]", encoding="utf-8")

        report = self.service.restore()

        self.assertTrue(report.ok, report.summary)
        self.assertEqual(sorted(report.restored), ["Configuration", "knowledge"])
        self.assertEqual(_count(self.database.db_path), 100)
        self.assertEqual((self.config_folder / "watchers.json").read_text(encoding="utf-8"), "[]")
        self.assertTrue(report.set_aside["knowledge"].exists())
        self.assertFalse(self.database.db_path.with_name("k.db-wal").exists())
        self.assertTrue(self.database.verify().ok)

    def test_a_restore_refuses_a_copy_that_fails_its_own_check(self) -> None:
        report = self.service.run()
        _damage(report.databases["knowledge"])

        restored = self.service.restore()

        self.assertFalse(restored.ok)
        self.assertIn("knowledge", restored.failures)
        self.assertEqual(_count(self.database.db_path), 100)

    def test_restoring_with_nothing_backed_up_says_so(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.service.restore()

    def test_a_missing_database_is_skipped_not_failed(self) -> None:
        self.service.databases["ghost"] = SQLiteDatabase(self.root / "ghost.db")
        report = self.service.run()
        self.assertTrue(report.ok)
        self.assertNotIn("ghost", report.databases)

    def test_the_scheduled_job_reports_the_run(self) -> None:
        job = build_jobs(backups=self.service)[DATABASE_BACKUP]
        result = job({})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["databases"], ["knowledge"])
        self.assertFalse(result.notify)


class MigrationCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_fresh_database_is_not_copied(self) -> None:
        database = SQLiteDatabase(self.root / "knowledge.db")
        self.assertIsNone(ensure_schema(database))
        self.assertEqual(stored_version(database), SCHEMA_VERSION)

    def test_an_older_database_is_copied_before_it_migrates(self) -> None:
        database = SQLiteDatabase(self.root / "knowledge.db")
        KnowledgeGraph(database).records.add(
            MemoryRecord(kind=MemoryKind.FACT, topic="iris/design", content="WAL is safe on a local drive", source="henry")
        )
        with database.connect() as conn:
            conn.execute("UPDATE knowledge_meta SET value = '1' WHERE key = 'schema_version'")
            conn.commit()

        copy = ensure_schema(database)

        self.assertIsNotNone(copy)
        self.assertEqual(copy.name, f"knowledge.before-v{SCHEMA_VERSION}.db")
        self.assertEqual(stored_version(SQLiteDatabase(copy)), 1)
        self.assertEqual(stored_version(database), SCHEMA_VERSION)
        self.assertIsNone(ensure_schema(database))


if __name__ == "__main__":
    unittest.main()
