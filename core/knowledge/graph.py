# File: core/knowledge/graph.py

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from core.knowledge.links import (
    EVIDENCE_RELATIONS,
    GROUNDING_RELATIONS,
    LinkRepository,
    MemoryLink,
    MemoryRelation,
)
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.repository import MEMORY_COLUMNS, KnowledgeRepository, record_from_row
from core.storage.sqlite_database import SQLiteDatabase

_LOOKUP_CHUNK = 400


@dataclass(frozen=True)
class Evidence:
    supporting: list[tuple[MemoryRecord, MemoryLink]] = field(default_factory=list)
    contradicting: list[tuple[MemoryRecord, MemoryLink]] = field(default_factory=list)

    @property
    def balance(self) -> int:
        return len(self.supporting) - len(self.contradicting)


@dataclass(frozen=True)
class Explanation:
    record: MemoryRecord
    relation: MemoryRelation | None = None
    grounds: list["Explanation"] = field(default_factory=list)
    truncated: bool = False

    def flatten(self) -> list[MemoryRecord]:
        out = [self.record]
        for ground in self.grounds:
            out.extend(ground.flatten())
        return out


class KnowledgeGraph:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.records = KnowledgeRepository(database)
        self.links = LinkRepository(database)


    def record_outcome(self, observation_id: str, outcome: MemoryRecord) -> MemoryRecord:
        observation = self.records.get(observation_id)
        if observation is None:
            raise KnowledgeError(f"No such observation: {observation_id}")
        if outcome.kind is not MemoryKind.OUTCOME:
            raise KnowledgeError(f"Expected an outcome record, got {outcome.kind.value}")
        stored = self.records.add(outcome)
        self.links.link(stored.id, observation_id, MemoryRelation.OUTCOME_OF)
        return stored

    def record_outcomes(
        self, pairs: Sequence[tuple[str, MemoryRecord]]
    ) -> list[MemoryRecord]:
        wanted = list(pairs)
        if not wanted:
            return []
        for observation_id, outcome in wanted:
            if outcome.kind is not MemoryKind.OUTCOME:
                raise KnowledgeError(f"Expected an outcome record, got {outcome.kind.value}")
        known = self._existing_ids([observation_id for observation_id, _ in wanted])
        for observation_id, _ in wanted:
            if observation_id not in known:
                raise KnowledgeError(f"No such observation: {observation_id}")

        stored = self.records.add_many([outcome for _, outcome in wanted])
        self.links.add_many(
            [
                MemoryLink(
                    source_id=outcome.id,
                    target_id=observation_id,
                    relation=MemoryRelation.OUTCOME_OF,
                )
                for (observation_id, _), outcome in zip(wanted, stored)
            ]
        )
        return stored

    def _existing_ids(self, ids: Sequence[str]) -> set[str]:
        found: set[str] = set()
        unique = list(dict.fromkeys(ids))
        with self.records.database.connect() as conn:
            for start in range(0, len(unique), _LOOKUP_CHUNK):
                chunk = unique[start : start + _LOOKUP_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT id FROM memories WHERE id IN ({placeholders})", tuple(chunk)
                ).fetchall()
                found.update(str(row[0]) for row in rows)
        return found

    def outcomes_for(self, observation_ids: Sequence[str]) -> dict[str, MemoryRecord]:
        unique = list(dict.fromkeys(observation_ids))
        if not unique:
            return {}
        columns = ", ".join(f"m.{name.strip()}" for name in MEMORY_COLUMNS.split(","))
        found: dict[str, MemoryRecord] = {}
        with self.records.database.connect() as conn:
            for start in range(0, len(unique), _LOOKUP_CHUNK):
                chunk = unique[start : start + _LOOKUP_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT l.target_id, {columns} FROM memory_links l "
                    "JOIN memories m ON m.id = l.source_id "
                    f"WHERE l.relation = ? AND l.target_id IN ({placeholders}) "
                    "ORDER BY l.sequence",
                    (MemoryRelation.OUTCOME_OF.value, *chunk),
                ).fetchall()
                for row in rows:
                    key = str(row["target_id"])
                    if key not in found:
                        found[key] = record_from_row(row)
        return found

    def decisions_for(self, observation_ids: Sequence[str]) -> dict[str, list[MemoryRecord]]:
        unique = list(dict.fromkeys(observation_ids))
        if not unique:
            return {}
        columns = ", ".join(f"m.{name.strip()}" for name in MEMORY_COLUMNS.split(","))
        found: dict[str, list[MemoryRecord]] = {}
        with self.records.database.connect() as conn:
            for start in range(0, len(unique), _LOOKUP_CHUNK):
                chunk = unique[start : start + _LOOKUP_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT l.target_id, {columns} FROM memory_links l "
                    "JOIN memories m ON m.id = l.source_id "
                    f"WHERE l.relation = ? AND l.target_id IN ({placeholders}) "
                    "ORDER BY l.sequence",
                    (MemoryRelation.DECIDED_FROM.value, *chunk),
                ).fetchall()
                for row in rows:
                    found.setdefault(str(row["target_id"]), []).append(record_from_row(row))
        return found

    def outcome_for(self, observation_id: str) -> MemoryRecord | None:
        incoming = self.links.links_to(observation_id, MemoryRelation.OUTCOME_OF)
        if not incoming:
            return None
        return self.records.get(incoming[0].source_id)

    def open_observations(self, topic: str, limit: int = 50) -> list[MemoryRecord]:
        columns = ", ".join(f"m.{name.strip()}" for name in MEMORY_COLUMNS.split(","))
        with self.records.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM memories m "
                "WHERE m.topic = ? AND m.kind = ? AND m.status != ? "
                "AND NOT EXISTS (SELECT 1 FROM memory_links l "
                "                WHERE l.target_id = m.id AND l.relation = ?) "
                "ORDER BY m.created_at DESC, m.sequence DESC LIMIT ?",
                (
                    topic,
                    MemoryKind.OBSERVATION.value,
                    MemoryStatus.SUPERSEDED.value,
                    MemoryRelation.OUTCOME_OF.value,
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [record_from_row(row) for row in rows]


    def add_evidence(
        self,
        hypothesis_id: str,
        evidence: MemoryRecord,
        *,
        supports: bool,
        weight: float | None = None,
        note: str | None = None,
    ) -> MemoryRecord:
        hypothesis = self.records.get(hypothesis_id)
        if hypothesis is None:
            raise KnowledgeError(f"No such hypothesis: {hypothesis_id}")
        if hypothesis.kind is not MemoryKind.HYPOTHESIS:
            raise KnowledgeError(f"Evidence attaches to a hypothesis, not a {hypothesis.kind.value}")

        stored = self.records.get(evidence.id) or self.records.add(evidence)
        relation = MemoryRelation.SUPPORTED_BY if supports else MemoryRelation.CONTRADICTED_BY
        self.links.link(hypothesis_id, stored.id, relation, weight=weight, note=note)
        return stored

    def evidence_for(self, hypothesis_id: str) -> Evidence:
        supporting: list[tuple[MemoryRecord, MemoryLink]] = []
        contradicting: list[tuple[MemoryRecord, MemoryLink]] = []
        for link in self.links.links_from(hypothesis_id):
            if link.relation not in EVIDENCE_RELATIONS:
                continue
            record = self.records.get(link.target_id)
            if record is None:
                continue
            bucket = supporting if link.relation is MemoryRelation.SUPPORTED_BY else contradicting
            bucket.append((record, link))
        return Evidence(supporting=supporting, contradicting=contradicting)


    def explain(self, memory_id: str, *, max_depth: int = 4) -> Explanation:
        record = self.records.get(memory_id)
        if record is None:
            raise KnowledgeError(f"No such memory: {memory_id}")
        return self._explain(record, relation=None, depth=max_depth, seen={memory_id})

    def _explain(
        self,
        record: MemoryRecord,
        *,
        relation: MemoryRelation | None,
        depth: int,
        seen: set[str],
    ) -> Explanation:
        outgoing = [
            link for link in self.links.links_from(record.id) if link.relation in GROUNDING_RELATIONS
        ]
        if depth <= 0:
            return Explanation(record=record, relation=relation, truncated=bool(outgoing))

        grounds: list[Explanation] = []
        truncated = False
        for link in outgoing:
            if link.target_id in seen:
                truncated = True
                continue
            target = self.records.get(link.target_id)
            if target is None:
                continue
            seen.add(link.target_id)
            grounds.append(self._explain(target, relation=link.relation, depth=depth - 1, seen=seen))
        return Explanation(record=record, relation=relation, grounds=grounds, truncated=truncated)


def render_explanation(explanation: Explanation, *, indent: int = 0) -> str:
    prefix = "  " * indent
    lead = f"{prefix}- " if indent else ""
    because = f" ({explanation.relation.value.replace('_', ' ')})" if explanation.relation else ""
    line = f"{lead}{explanation.record.content}{because}"
    if explanation.truncated:
        line += " [...]"
    lines = [line]
    for ground in explanation.grounds:
        lines.append(render_explanation(ground, indent=indent + 1))
    return "\n".join(lines)
