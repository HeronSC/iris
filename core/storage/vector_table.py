# File: core/storage/vector_table.py

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from core.storage.sqlite_database import SQLiteDatabase

logger = logging.getLogger(__name__)

try:
    #! @allow-local-import
    import sqlite_vec as _sqlite_vec
except Exception:
    _sqlite_vec = None

META_TABLE = "vector_meta"


def extension_available() -> bool:
    return _sqlite_vec is not None


class VectorTable:
    def __init__(
        self,
        database: SQLiteDatabase,
        table: str,
        dimensions: int,
        *,
        model_stamp: str,
        key_column: str = "id",
        isolate_writes: bool = True,
        write_timeout_seconds: float = 120.0,
    ) -> None:
        if not table.isidentifier() or not key_column.isidentifier():
            raise ValueError("table and key column must be plain identifiers")
        self.database = database
        self.table = table
        self.dimensions = int(dimensions)
        self.model_stamp = model_stamp
        self.key_column = key_column
        self.isolate_writes = isolate_writes
        self.write_timeout_seconds = write_timeout_seconds
        self._available: bool | None = None


    @property
    def available(self) -> bool:
        if _sqlite_vec is None:
            return False
        if self._available is None:
            try:
                with self.connect() as conn:
                    conn.execute("SELECT vec_version()").fetchone()
                self._available = True
            except Exception as error:
                logger.warning("sqlite-vec is not usable here: %s", error)
                self._available = False
        return self._available

    def connect(self) -> sqlite3.Connection:
        conn = self.database.connect()
        try:
            conn.enable_load_extension(True)
            _sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            conn.close()
            raise
        return conn


    def ensure_schema(self) -> bool:
        rebuilt = False
        with self.connect() as conn:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {META_TABLE} (table_name TEXT PRIMARY KEY, model_stamp TEXT NOT NULL)")
            row = conn.execute(f"SELECT model_stamp FROM {META_TABLE} WHERE table_name = ?", (self.table,)).fetchone()
            current = str(row[0]) if row is not None else None
            if current is not None and current != self.model_stamp:
                logger.info("Vector table %s: model changed from %s to %s; rebuilding", self.table, current, self.model_stamp)
                conn.execute(f"DROP TABLE IF EXISTS {self.table}")
                rebuilt = True
            elif current is None:
                rebuilt = True
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.table} USING vec0("
                f"{self.key_column} INTEGER PRIMARY KEY, "
                f"embedding float[{self.dimensions}] distance_metric=cosine)"
            )
            conn.execute(
                f"INSERT INTO {META_TABLE}(table_name, model_stamp) VALUES(?, ?) "
                "ON CONFLICT(table_name) DO UPDATE SET model_stamp = excluded.model_stamp",
                (self.table, self.model_stamp),
            )
            conn.commit()
        return rebuilt


    def store(self, rows: list[tuple[int, list[float]]]) -> None:
        if not rows:
            return
        if not self.isolate_writes:
            with self.connect() as conn:
                conn.executemany(
                    f"INSERT OR REPLACE INTO {self.table}({self.key_column}, embedding) VALUES (?, ?)",
                    [(int(key), self.serialize(vector)) for key, vector in rows],
                )
                conn.commit()
            return
        payload = "".join(json.dumps([int(key), [float(value) for value in vector]]) + "\n" for key, vector in rows)
        completed = subprocess.run(
            [sys.executable, "-m", "core.storage.vec_writer", str(self.database.db_path), self.table, self.key_column],
            input=payload,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[2]),
            timeout=self.write_timeout_seconds,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()[-1:] or [f"exit code {completed.returncode}"]
            raise RuntimeError(f"vector writer failed: {detail[0]}")

    def delete(self, keys: list[int]) -> None:
        if not keys:
            return
        with self.connect() as conn:
            conn.executemany(f"DELETE FROM {self.table} WHERE {self.key_column} = ?", [(int(key),) for key in keys])
            conn.commit()


    def search(self, vector: list[float], k: int) -> list[tuple[int, float]]:
        if k <= 0:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {self.key_column}, distance FROM {self.table} WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (self.serialize(vector), int(k)),
            ).fetchall()
        return [(int(row[0]), 1.0 - float(row[1])) for row in rows]

    def count(self) -> int:
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()
        return int(row[0]) if row else 0

    def keys(self) -> set[int]:
        with self.connect() as conn:
            return {int(row[0]) for row in conn.execute(f"SELECT {self.key_column} FROM {self.table}").fetchall()}

    @staticmethod
    def serialize(vector: list[float]) -> bytes:
        return _sqlite_vec.serialize_float32([float(value) for value in vector])


def relevance(similarity: float, floor: float, ceiling: float) -> float:
    span = max(1e-6, ceiling - floor)
    return max(0.0, min(1.0, (similarity - floor) / span))
