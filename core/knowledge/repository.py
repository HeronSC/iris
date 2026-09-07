from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.schema import ensure_schema, next_sequence
from core.storage.sqlite_database import SQLiteDatabase


MEMORY_COLUMNS = (
    "id, kind, topic, status, content, data_json, confidence, source, "
    "source_ref, occurred_at, created_at, supersedes, superseded_by"
)


class KnowledgeRepository:
    """Append-only store for what Iris knows.

    ``add`` and ``supersede`` are the only ways in. There is deliberately no
    way to edit a record's content: the audit question "why does Iris believe
    this" is only answerable if what it believed earlier is still on disk.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        ensure_schema(database)

    def add(self, record: MemoryRecord) -> MemoryRecord:
        with self.database.connect() as conn:
            _insert(conn, record)
            conn.commit()
        return record

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self.database.connect() as conn:
            row = conn.execute(f"SELECT {MEMORY_COLUMNS} FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return record_from_row(row) if row is not None else None

    def supersede(self, memory_id: str, replacement: MemoryRecord) -> MemoryRecord:
        """Replace a record with a corrected one, keeping the original readable.

        The old record keeps its content and gains status ``superseded``; the
        new one records what it replaced. Both happen in one transaction so a
        failure cannot leave a dangling pointer.
        """
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
        """The one permitted in-place change. Content stays immutable."""
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
        """The chain of revisions ending at this record, oldest first."""
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
        """Filtered in SQL, not in Python: this is the shape retrieval will grow from."""
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
        """Records whose id starts with this, so a person can type the first few characters.

        A range comparison rather than LIKE, so the primary key index serves it.
        Returns several when the prefix is ambiguous; the caller decides.
        """
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

    def list_by_kind_and_status(
        self, kind: MemoryKind, status: MemoryStatus, *, limit: int = 50
    ) -> list[MemoryRecord]:
        """Every record of one kind in one state, across topics.

        Served by idx_memories_kind_status, so asking "what is waiting on me"
        does not scan.
        """
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {MEMORY_COLUMNS} FROM memories WHERE kind = ? AND status = ? "
                "ORDER BY created_at DESC, sequence DESC LIMIT ?",
                (MemoryKind(kind).value, MemoryStatus(status).value, max(1, int(limit))),
            ).fetchall()
        return [record_from_row(row) for row in rows]

    def count(self) -> int:
        with self.database.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])


def _insert(conn: Any, record: MemoryRecord) -> None:
    """Write one record, reporting a clash as a KnowledgeError rather than a sqlite one.

    Callers see the same failure whether they came through add or supersede,
    and the enclosing transaction still rolls back on the way out.
    """
    row = record.to_row()
    row["data_json"] = json.dumps(row["data_json"], ensure_ascii=False)
    if conn.execute("SELECT 1 FROM memories WHERE id = ?", (record.id,)).fetchone() is not None:
        raise KnowledgeError(f"Memory already exists: {record.id}")
    row["sequence"] = next_sequence(conn, "memories")
    try:
        conn.execute(
            f"INSERT INTO memories (sequence, {MEMORY_COLUMNS}) VALUES "
            "(:sequence, :id, :kind, :topic, :status, :content, :data_json, :confidence, :source, "
            ":source_ref, :occurred_at, :created_at, :supersedes, :superseded_by)",
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
    }
    values.update(changes)
    return MemoryRecord(**values)

