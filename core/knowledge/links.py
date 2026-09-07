# File: core/knowledge/links.py

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from core.knowledge.models import KnowledgeError, utc_now_iso
from core.knowledge.schema import ensure_schema, next_sequence
from core.storage.sqlite_database import SQLiteDatabase


class MemoryRelation(str, Enum):
    SUPPORTED_BY = "supported_by"
    CONTRADICTED_BY = "contradicted_by"
    DERIVED_FROM = "derived_from"
    DECIDED_FROM = "decided_from"
    OUTCOME_OF = "outcome_of"
    RELATES_TO = "relates_to"


EVIDENCE_RELATIONS = (MemoryRelation.SUPPORTED_BY, MemoryRelation.CONTRADICTED_BY)

GROUNDING_RELATIONS = (
    MemoryRelation.SUPPORTED_BY,
    MemoryRelation.CONTRADICTED_BY,
    MemoryRelation.DERIVED_FROM,
    MemoryRelation.DECIDED_FROM,
    MemoryRelation.OUTCOME_OF,
)


@dataclass(frozen=True)
class MemoryLink:
    source_id: str
    target_id: str
    relation: MemoryRelation
    id: str = field(default_factory=lambda: uuid4().hex)
    weight: float | None = None
    note: str | None = None
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not isinstance(self.relation, MemoryRelation):
            object.__setattr__(self, "relation", MemoryRelation(str(self.relation)))
        if not str(self.source_id).strip() or not str(self.target_id).strip():
            raise KnowledgeError("A link needs both ends")
        if self.source_id == self.target_id:
            raise KnowledgeError("A memory cannot link to itself")


_COLUMNS = "id, source_id, target_id, relation, weight, note, created_at"


class LinkRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        ensure_schema(database)

    def add(self, link: MemoryLink) -> MemoryLink:
        row = {
            "id": link.id,
            "source_id": link.source_id,
            "target_id": link.target_id,
            "relation": link.relation.value,
            "weight": link.weight,
            "note": link.note,
            "created_at": link.created_at,
        }
        with self.database.connect() as conn:
            row["sequence"] = next_sequence(conn, "memory_links")
            try:
                conn.execute(
                    f"INSERT INTO memory_links (sequence, {_COLUMNS}) VALUES "
                    "(:sequence, :id, :source_id, :target_id, :relation, :weight, :note, :created_at)",
                    row,
                )
            except sqlite3.IntegrityError as error:
                raise KnowledgeError(_explain_integrity_error(error, link)) from error
            conn.commit()
        return link

    def link(
        self,
        source_id: str,
        target_id: str,
        relation: MemoryRelation,
        *,
        weight: float | None = None,
        note: str | None = None,
    ) -> MemoryLink:
        return self.add(
            MemoryLink(source_id=source_id, target_id=target_id, relation=relation, weight=weight, note=note)
        )

    def links_from(self, source_id: str, relation: MemoryRelation | None = None) -> list[MemoryLink]:
        return self._query("source_id", source_id, relation)

    def links_to(self, target_id: str, relation: MemoryRelation | None = None) -> list[MemoryLink]:
        return self._query("target_id", target_id, relation)

    def _query(self, column: str, value: str, relation: MemoryRelation | None) -> list[MemoryLink]:
        clauses = [f"{column} = ?"]
        params: list[Any] = [value]
        if relation is not None:
            clauses.append("relation = ?")
            params.append(MemoryRelation(relation).value)
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM memory_links WHERE {' AND '.join(clauses)} ORDER BY sequence",
                params,
            ).fetchall()
        return [_link_from_row(row) for row in rows]

    def count_relations_from(self, source_id: str) -> dict[MemoryRelation, int]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT relation, COUNT(*) FROM memory_links WHERE source_id = ? GROUP BY relation",
                (source_id,),
            ).fetchall()
        return {MemoryRelation(str(row[0])): int(row[1]) for row in rows}

    def count(self) -> int:
        with self.database.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0])


def _explain_integrity_error(error: sqlite3.IntegrityError, link: MemoryLink) -> str:
    text = str(error).lower()
    if "foreign key" in text:
        return (
            f"Cannot link {link.source_id} -> {link.target_id}: "
            "both ends must be existing memories"
        )
    if "unique" in text:
        return (
            f"That link already exists: {link.source_id} -{link.relation.value}-> {link.target_id}"
        )
    return f"Could not store link {link.id}: {error}"


def _link_from_row(row: Any) -> MemoryLink:
    return MemoryLink(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        target_id=str(row["target_id"]),
        relation=MemoryRelation(str(row["relation"])),
        weight=row["weight"],
        note=row["note"],
        created_at=str(row["created_at"]),
    )
