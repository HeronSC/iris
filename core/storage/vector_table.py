# File: core/storage/vector_table.py

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from core.storage.sqlite_database import SQLiteDatabase

try:
    import numpy as _np
    from usearch.index import Index as _Index
except ImportError:
    _np = None
    _Index = None

logger = logging.getLogger(__name__)

META_TABLE = "vector_meta"


def extension_available() -> bool:
    return _Index is not None and _np is not None


def relevance(similarity: float, floor: float, ceiling: float) -> float:
    span = max(1e-6, ceiling - floor)
    return max(0.0, min(1.0, (similarity - floor) / span))


class VectorTable:
    def __init__(
        self,
        database: SQLiteDatabase,
        table: str,
        dimensions: int,
        *,
        model_stamp: str,
        key_column: str = "id",
    ) -> None:
        if not table.isidentifier() or not key_column.isidentifier():
            raise ValueError("table and key column must be plain identifiers")
        self.database = database
        self.table = table
        self.dimensions = int(dimensions)
        self.model_stamp = model_stamp
        self.key_column = key_column
        self.index_path = Path(database.db_path).with_name(f"{Path(database.db_path).stem}.{table}.usearch")
        self._index: Any = None
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return extension_available()

    def ensure_schema(self) -> bool:
        with self._lock:
            with self.database.connect() as conn:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {META_TABLE} (table_name TEXT PRIMARY KEY, model_stamp TEXT NOT NULL)")
                row = conn.execute(f"SELECT model_stamp FROM {META_TABLE} WHERE table_name = ?", (self.table,)).fetchone()
                current = str(row[0]) if row is not None else None
                rebuilt = current != self.model_stamp
                if current is not None and rebuilt:
                    logger.info("Vector table %s: model changed from %s to %s; rebuilding", self.table, current, self.model_stamp)
                conn.execute(
                    f"INSERT INTO {META_TABLE}(table_name, model_stamp) VALUES(?, ?) "
                    "ON CONFLICT(table_name) DO UPDATE SET model_stamp = excluded.model_stamp",
                    (self.table, self.model_stamp),
                )
                conn.commit()
            if rebuilt:
                self._index = self._new_index()
                if self.index_path.exists():
                    self.index_path.unlink()
                self._save()
            else:
                self._load()
            return rebuilt

    def store(self, rows: list[tuple[int, list[float]]]) -> None:
        if not rows:
            return
        with self._lock:
            index = self._ensure_loaded()
            keys = [int(key) for key, _ in rows]
            present = [key for key in keys if key in index]
            if present:
                index.remove(_np.array(present, dtype=_np.uint64))
            index.add(
                _np.array(keys, dtype=_np.uint64),
                _np.array([[float(value) for value in vector] for _, vector in rows], dtype=_np.float32),
            )
            self._save()

    def delete(self, keys: list[int]) -> None:
        if not keys:
            return
        with self._lock:
            index = self._ensure_loaded()
            present = [int(key) for key in keys if int(key) in index]
            if present:
                index.remove(_np.array(present, dtype=_np.uint64))
                self._save()

    def search(self, vector: list[float], k: int) -> list[tuple[int, float]]:
        if k <= 0:
            return []
        with self._lock:
            index = self._ensure_loaded()
            if len(index) == 0:
                return []
            matches = index.search(_np.array([float(value) for value in vector], dtype=_np.float32), min(int(k), len(index)))
        return [(int(key), 1.0 - float(distance)) for key, distance in zip(matches.keys.tolist(), matches.distances.tolist())]

    def count(self) -> int:
        with self._lock:
            return len(self._ensure_loaded())

    def keys(self) -> set[int]:
        with self._lock:
            return {int(key) for key in self._ensure_loaded().keys}

    def _new_index(self) -> Any:
        return _Index(ndim=self.dimensions, metric="cos", dtype="f32")

    def _ensure_loaded(self) -> Any:
        if self._index is None:
            self._load()
        return self._index

    def _load(self) -> None:
        index = self._new_index()
        if self.index_path.exists():
            try:
                index.load(str(self.index_path))
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Vector index %s unreadable, starting empty: %s", self.index_path, error)
                index = self._new_index()
        self._index = index

    def _save(self) -> None:
        if self._index is None:
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index.save(str(self.index_path))
