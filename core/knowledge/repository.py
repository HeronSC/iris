# File: core/knowledge/repository.py

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.schema import ensure_schema, next_sequence
from core.storage.sqlite_database import SQLiteDatabase


MEMORY_COLUMNS = (
    "id, kind, topic, status, content, data_json, confidence, source, "
    "source_ref, occurred_at, created_at, supersedes, superseded_by, scope"
)

_ID_CHECK_CHUNK = 400


class KnowledgeRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        ensure_schema(database)

    def add(self, record: MemoryRecord) -> MemoryRecord:
        return self.add_many((record,))[0]

    def add_many(self, records: Sequence[MemoryRecord]) -> list[MemoryRecord]:
        stored = list(records)
        if not stored:
            return []

        _refuse_repeats_within(stored)
        rows = [_row_for(record) for record in stored]
        with self.database.connect() as conn:
            _refuse_ids_already_stored(conn, [record.id for record in stored])
            base = next_sequence(conn, "memories")
            for offset, row in enumerate(rows):
                row["sequence"] = base + offset
            try:
                conn.executemany(
                    f"INSERT INTO memories (sequence, {MEMORY_COLUMNS}) VALUES "
                    "(:sequence, :id, :kind, :topic, :status, :content, :data_json, :confidence, "
                    ":source, :source_ref, :occurred_at, :created_at, :supersedes, :superseded_by, :scope)",
                    rows,
                )
            except sqlite3.IntegrityError as error:
                raise KnowledgeError(f"Could not store {len(rows)} memories: {error}") from error
            conn.commit()
        return stored

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self.database.connect() as conn:
            row = conn.execute(f"SELECT {MEMORY_COLUMNS} FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return record_from_row(row) if row is not None else None

    def supersede(self, memory_id: str, replacement: MemoryRecord) -> MemoryRecord:
        if replacement.id == memory_id:
            raise KnowledgeError("A memory cannot supersede itself")

        stored = _replace(replacement, supersedes=memory_id)
        with self.database.connect() as conn:
            original = conn.execute("SELECT status FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if original is None:
                raise KnowledgeError(f"Cannot supersede a memory that does not exist: {memory_id}")
            if str(original["status"]) == MemoryStatus.SUPERSEDED.value:
                raise KnowledgeError(f"Memory is already superseded: {memory_id}")
            _insert(conn, stored)
            conn.execute(
                "UPDATE memories SET status = ?, superseded_by = ? WHERE id = ?",
                (MemoryStatus.SUPERSEDED.value, replacement.id, memory_id),
            )
            conn.commit()
        return stored

    def set_status(self, memory_id: str, status: MemoryStatus) -> None:
        if not isinstance(status, MemoryStatus):
            status = MemoryStatus(str(status))
        with self.database.connect() as conn:
            cursor = conn.execute(
                "UPDATE memories SET status = ? WHERE id = ?", (status.value, memory_id)
            )
            if cursor.rowcount == 0:
                raise KnowledgeError(f"No such memory: {memory_id}")
            conn.commit()

    def history(self, memory_id: str) -> list[MemoryRecord]:
        chain: list[MemoryRecord] = []
        seen: set[str] = set()
        current = self.get(memory_id)
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            current = self.get(current.supersedes) if current.supersedes else None
        return list(reversed(chain))

    def list_by_topic(
        self,
        topic: str,
        *,
        kind: MemoryKind | None = None,
        include_superseded: bool = False,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        clauses = ["topic = ?"]
        params: list[Any] = [topic]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(MemoryKind(kind).value)
        if not include_superseded:
            clauses.append("status != ?")
            params.append(MemoryStatus.SUPERSEDED.value)
        params.append(max(1, int(limit)))
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {MEMORY_COLUMNS} FROM memories WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, sequence DESC LIMIT ?",
                params,
            ).fetchall()
        return [record_from_row(row) for row in rows]

    def find_by_id_prefix(self, prefix: str, *, limit: int = 5) -> list[MemoryRecord]:
        cleaned = str(prefix or "").strip().lower()
        if not cleaned:
            return []
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {MEMORY_COLUMNS} FROM memories WHERE id >= ? AND id < ? "
                "ORDER BY sequence DESC LIMIT ?",
                (cleaned, cleaned + "￿", max(1, int(limit))),
            ).fetchall()
        return [record_from_row(row) for row in rows]

    def list_by_source_ref(
        self, source_ref: str, *, kind: MemoryKind | None = None, limit: int = 2000
    ) -> list[MemoryRecord]:
        clauses = ["source_ref = ?"]
        params: list[Any] = [source_ref]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(MemoryKind(kind).value)
        params.append(max(1, int(limit)))
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {MEMORY_COLUMNS} FROM memories WHERE {' AND '.join(clauses)} "
                "ORDER BY sequence LIMIT ?",
                params,
            ).fetchall()
        return [record_from_row(row) for row in rows]

    def list_by_kind_and_status(
        self, kind: MemoryKind, status: MemoryStatus, *, limit: int = 50
    ) -> list[MemoryRecord]:
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {MEMORY_COLUMNS} FROM memories WHERE kind = ? AND status = ? "
                "ORDER BY created_at DESC, sequence DESC LIMIT ?",
                (MemoryKind(kind).value, MemoryStatus(status).value, max(1, int(limit))),
            ).fetchall()
        return [record_from_row(row) for row in rows]

    def count_by_scope(self) -> dict[str, int]:
        with self.database.connect() as conn:
            rows = conn.execute("SELECT scope, COUNT(*) FROM memories WHERE status != 'superseded' GROUP BY scope ORDER BY scope").fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def count(self) -> int:
        with self.database.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def list_all(self, *, include_superseded: bool = False, limit: int = 100000, offset: int = 0) -> list[MemoryRecord]:
        clause = "" if include_superseded else "WHERE status != ?"
        params: list[Any] = [] if include_superseded else [MemoryStatus.SUPERSEDED.value]
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {MEMORY_COLUMNS} FROM memories {clause} ORDER BY topic, sequence LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [record_from_row(row) for row in rows]


def _row_for(record: MemoryRecord) -> dict[str, Any]:
    row = record.to_row()
    row["data_json"] = json.dumps(row["data_json"], ensure_ascii=False)
    return row


def _refuse_repeats_within(records: Sequence[MemoryRecord]) -> None:
    seen: set[str] = set()
    for record in records:
        if record.id in seen:
            raise KnowledgeError(f"The same id appears twice in one batch: {record.id}")
        seen.add(record.id)


def _refuse_ids_already_stored(conn: Any, ids: Sequence[str]) -> None:
    for start in range(0, len(ids), _ID_CHECK_CHUNK):
        chunk = ids[start : start + _ID_CHECK_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        row = conn.execute(
            f"SELECT id FROM memories WHERE id IN ({placeholders}) LIMIT 1", tuple(chunk)
        ).fetchone()
        if row is not None:
            raise KnowledgeError(f"Memory already exists: {str(row[0])}")


def _insert(conn: Any, record: MemoryRecord) -> None:
    row = _row_for(record)
    _refuse_ids_already_stored(conn, (record.id,))
    row["sequence"] = next_sequence(conn, "memories")
    try:
        conn.execute(
            f"INSERT INTO memories (sequence, {MEMORY_COLUMNS}) VALUES "
            "(:sequence, :id, :kind, :topic, :status, :content, :data_json, :confidence, :source, "
            ":source_ref, :occurred_at, :created_at, :supersedes, :superseded_by, :scope)",
            row,
        )
    except sqlite3.IntegrityError as error:
        raise KnowledgeError(f"Could not store memory {record.id}: {error}") from error


def record_from_row(row: Any) -> MemoryRecord:
    try:
        data = json.loads(row["data_json"])
    except (TypeError, ValueError):
        data = {}
    return MemoryRecord(
        id=str(row["id"]),
        kind=MemoryKind(str(row["kind"])),
        topic=str(row["topic"]),
        status=MemoryStatus(str(row["status"])),
        content=str(row["content"]),
        data=data if isinstance(data, dict) else {},
        confidence=row["confidence"],
        source=str(row["source"]),
        source_ref=row["source_ref"],
        occurred_at=row["occurred_at"],
        created_at=str(row["created_at"]),
        supersedes=row["supersedes"],
        superseded_by=row["superseded_by"],
        scope=str(row["scope"]) if "scope" in row.keys() and row["scope"] else "global",
    )


def _replace(record: MemoryRecord, **changes: Any) -> MemoryRecord:
    values = {
        "id": record.id,
        "kind": record.kind,
        "topic": record.topic,
        "status": record.status,
        "content": record.content,
        "data": record.data,
        "confidence": record.confidence,
        "source": record.source,
        "source_ref": record.source_ref,
        "occurred_at": record.occurred_at,
        "created_at": record.created_at,
        "supersedes": record.supersedes,
        "superseded_by": record.superseded_by,
        "scope": record.scope,
    }
    values.update(changes)
    return MemoryRecord(**values)

