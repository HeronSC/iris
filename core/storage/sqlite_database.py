# File: core/storage/sqlite_database.py

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000

JOURNAL_MODE = "wal"

SIDECAR_SUFFIXES = ("-wal", "-shm")

QUICK_CHECK_MESSAGES = 8


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_val, exc_tb):
        result = super().__exit__(exc_type, exc_val, exc_tb)
        self.close()
        return result


class DatabaseDamaged(sqlite3.DatabaseError):
    pass


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    messages: tuple[str, ...]
    checked_at: str

    @property
    def summary(self) -> str:
        if self.ok:
            return "ok"
        return "; ".join(self.messages) or "quick_check failed"


class SQLiteDatabase:
    def __init__(self, db_path: str | Path, *, verify_on_first_use: bool = True) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.verify_on_first_use = verify_on_first_use
        self._integrity: IntegrityReport | None = None
        self._lock = threading.Lock()
        self._journal_warned = False

    @property
    def integrity(self) -> IntegrityReport | None:
        return self._integrity

    @property
    def damaged(self) -> bool:
        return self._integrity is not None and not self._integrity.ok

    def connect(self) -> sqlite3.Connection:
        if self.verify_on_first_use and self._integrity is None:
            self.verify()
        if self.damaged:
            return self._connect_read_only()
        connection = sqlite3.connect(self.db_path, factory=ManagedConnection)
        connection.row_factory = sqlite3.Row
        mode = str(connection.execute(f"PRAGMA journal_mode={JOURNAL_MODE}").fetchone()[0]).lower()
        if mode != JOURNAL_MODE and not self._journal_warned:
            self._journal_warned = True
            logger.warning("%s could not switch to WAL and is in %s mode", self.db_path.name, mode)
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, factory=ManagedConnection)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return connection

    def verify(self, *, force: bool = False) -> IntegrityReport:
        with self._lock:
            if self._integrity is not None and not force:
                return self._integrity
            checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            if not self.db_path.exists() or self.db_path.stat().st_size == 0:
                self._integrity = IntegrityReport(ok=True, messages=(), checked_at=checked_at)
                return self._integrity
            try:
                connection = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
                try:
                    rows = connection.execute(f"PRAGMA quick_check({QUICK_CHECK_MESSAGES})").fetchall()
                finally:
                    connection.close()
                messages = tuple(str(row[0]) for row in rows if str(row[0]).lower() != "ok")
            except sqlite3.DatabaseError as error:
                messages = (str(error),)
            report = IntegrityReport(ok=not messages, messages=messages, checked_at=checked_at)
            if not report.ok:
                logger.error("%s failed quick_check and is now read-only: %s", self.db_path, report.messages[0][:200] if report.messages else "unknown")
            self._integrity = report
            return report

    def require_healthy(self) -> None:
        report = self.verify()
        if not report.ok:
            raise DatabaseDamaged(f"{self.db_path.name} failed quick_check: {report.summary}")

    def journal_mode(self) -> str:
        with self.connect() as conn:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def backup_to(self, destination: str | Path) -> Path:
        target = Path(destination).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("",) + SIDECAR_SUFFIXES:
            stale = target.with_name(target.name + suffix)
            if stale.exists():
                stale.unlink()
        source = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
        try:
            copy = sqlite3.connect(target)
            try:
                source.backup(copy)
                copy.execute("PRAGMA journal_mode=DELETE")
            finally:
                copy.close()
        finally:
            source.close()
        return target

    def sidecars(self) -> list[Path]:
        return [self.db_path.with_name(self.db_path.name + suffix) for suffix in SIDECAR_SUFFIXES]


def remove_sidecars(db_path: Path) -> None:
    for suffix in SIDECAR_SUFFIXES:
        stale = db_path.with_name(db_path.name + suffix)
        if stale.exists():
            stale.unlink()


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DatabaseDamaged",
    "IntegrityReport",
    "JOURNAL_MODE",
    "SQLiteDatabase",
    "remove_sidecars",
]
