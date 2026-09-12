# File: core/knowledge/facts.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.ranking import tokenize
from core.knowledge.repository import KnowledgeRepository
from core.knowledge.scopes import GLOBAL

CONFLICT_THRESHOLD = 0.45
PRUNABLE_KINDS = (MemoryKind.OBSERVATION, MemoryKind.OUTCOME)
KEPT_FOREVER = (MemoryKind.FACT, MemoryKind.DECISION, MemoryKind.HYPOTHESIS, MemoryKind.KNOWLEDGE)
HIDDEN_STATUSES = (MemoryStatus.SUPERSEDED, MemoryStatus.RETIRED)

POLICY = (
    "Facts, decisions, hypotheses and knowledge are never pruned; a wrong one is superseded or retired by hand, and the old record stays. "
    "Observations and outcomes fade in ranking as they age and can be retired in bulk with /knowledge prune <days>, after a preview. "
    "Retired and superseded records stay in the database and the export but are not recalled."
)


@dataclass(frozen=True)
class Conflict:
    record: MemoryRecord
    similarity: float

    def describe(self) -> str:
        return f"{self.record.id[:8]} ({int(round(self.similarity * 100))}% alike, {self.record.status.value}, {self.record.created_at[:10]}): {self.record.content[:100]}"


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def similarity(left: str, right: str) -> float:
    a = tokenize(left)
    b = tokenize(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_conflicts(repository: KnowledgeRepository, topic: str, content: str, *, scope: str = GLOBAL, threshold: float = CONFLICT_THRESHOLD, exclude_id: str | None = None) -> list[Conflict]:
    found: list[Conflict] = []
    wanted = _normalized(content)
    for record in repository.list_by_topic(topic, kind=MemoryKind.FACT, limit=200):
        if record.id == exclude_id or record.status in HIDDEN_STATUSES:
            continue
        if record.scope not in {GLOBAL, scope}:
            continue
        if _normalized(record.content) == wanted:
            found.append(Conflict(record=record, similarity=1.0))
            continue
        score = similarity(record.content, content)
        if score >= threshold:
            found.append(Conflict(record=record, similarity=score))
    found.sort(key=lambda item: (-item.similarity, item.record.created_at))
    return found


class FactService:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    def record(self, topic: str, content: str, *, source: str, scope: str = GLOBAL, supersedes: str | None = None) -> tuple[MemoryRecord, list[Conflict]]:
        clean = " ".join(content.split())
        if not clean:
            raise KnowledgeError("The fact is empty")
        normalized_topic = topic.strip().lower()
        if supersedes:
            old = self.repository.get(supersedes)
            if old is None:
                raise KnowledgeError(f"No such memory: {supersedes}")
            replacement = MemoryRecord(kind=old.kind, topic=old.topic, content=clean, source=source, scope=old.scope, data={"replaces": old.content[:300]})
            stored = self.repository.supersede(old.id, replacement)
            return stored, []
        conflicts = find_conflicts(self.repository, normalized_topic, clean, scope=scope)
        if any(item.similarity >= 1.0 for item in conflicts):
            same = next(item for item in conflicts if item.similarity >= 1.0)
            raise KnowledgeError(f"Already recorded as {same.record.id[:8]}: {same.record.content[:80]}")
        stored = self.repository.add(MemoryRecord(kind=MemoryKind.FACT, topic=normalized_topic, content=clean, source=source, scope=scope))
        return stored, conflicts

    def supersede(self, memory_id: str, content: str, *, source: str) -> MemoryRecord:
        stored, _conflicts = self.record("", content, source=source, supersedes=memory_id)
        return stored

    def retire(self, memory_id: str, *, reason: str) -> MemoryRecord:
        record = self.repository.get(memory_id)
        if record is None:
            raise KnowledgeError(f"No such memory: {memory_id}")
        if record.status in HIDDEN_STATUSES:
            raise KnowledgeError(f"{memory_id[:8]} is already {record.status.value}")
        if not reason.strip():
            raise KnowledgeError("Say why it is being forgotten; the reason is kept with the record's history")
        self.repository.set_status(record.id, MemoryStatus.RETIRED)
        return self.repository.get(record.id) or record

    def prune_candidates(self, days: int, *, now: datetime | None = None, limit: int = 5000) -> list[MemoryRecord]:
        if days < 1:
            raise KnowledgeError("Give the age in days, at least 1")
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
        candidates: list[MemoryRecord] = []
        for record in self.repository.list_all(limit=limit):
            if record.kind not in PRUNABLE_KINDS or record.status in HIDDEN_STATUSES:
                continue
            stamp = record.occurred_at or record.created_at
            if stamp < cutoff:
                candidates.append(record)
        return candidates

    def prune(self, days: int, *, now: datetime | None = None) -> list[MemoryRecord]:
        candidates = self.prune_candidates(days, now=now)
        for record in candidates:
            self.repository.set_status(record.id, MemoryStatus.RETIRED)
        return candidates

    def browse(self, topic: str | None = None, *, limit: int = 20, kinds: Sequence[MemoryKind] = ()) -> list[MemoryRecord]:
        if topic:
            records = self.repository.list_by_topic(topic.strip().lower(), limit=limit)
        else:
            records = self.repository.recent(limit=limit)
        if kinds:
            records = [record for record in records if record.kind in kinds]
        return records


def describe_record(record: MemoryRecord) -> str:
    scope = "" if record.scope == GLOBAL else f" [{record.scope}]"
    return f"{record.id[:8]}  {record.kind.value:<11} {record.status.value:<10} {record.created_at[:10]}  {record.topic}{scope}: {record.content[:80]}"


def to_json(record: MemoryRecord) -> dict[str, Any]:
    return {"id": record.id, "kind": record.kind.value, "status": record.status.value, "topic": record.topic, "scope": record.scope, "content": record.content, "created_at": record.created_at}


__all__ = ["CONFLICT_THRESHOLD", "Conflict", "FactService", "HIDDEN_STATUSES", "KEPT_FOREVER", "POLICY", "PRUNABLE_KINDS", "describe_record", "find_conflicts", "similarity", "to_json"]
