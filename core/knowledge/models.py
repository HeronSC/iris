# File: core/knowledge/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class KnowledgeError(ValueError):
    pass


class MemoryKind(str, Enum):

    FACT = "fact"
    OBSERVATION = "observation"
    OUTCOME = "outcome"
    DECISION = "decision"
    HYPOTHESIS = "hypothesis"
    KNOWLEDGE = "knowledge"


class MemoryStatus(str, Enum):
    OBSERVED = "observed"
    PROPOSED = "proposed"
    TESTING = "testing"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


DEFAULT_STATUS: dict[MemoryKind, MemoryStatus] = {
    MemoryKind.FACT: MemoryStatus.ACCEPTED,
    MemoryKind.OBSERVATION: MemoryStatus.OBSERVED,
    MemoryKind.OUTCOME: MemoryStatus.OBSERVED,
    MemoryKind.DECISION: MemoryStatus.OBSERVED,
    MemoryKind.HYPOTHESIS: MemoryStatus.PROPOSED,
    MemoryKind.KNOWLEDGE: MemoryStatus.ACCEPTED,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class MemoryRecord:

    kind: MemoryKind
    topic: str
    content: str
    id: str = field(default_factory=lambda: uuid4().hex)
    status: MemoryStatus | None = None
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    source: str = "unknown"
    source_ref: str | None = None
    occurred_at: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    supersedes: str | None = None
    superseded_by: str | None = None
    scope: str = "global"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MemoryKind):
            object.__setattr__(self, "kind", MemoryKind(str(self.kind)))
        if self.status is None:
            object.__setattr__(self, "status", DEFAULT_STATUS[self.kind])
        elif not isinstance(self.status, MemoryStatus):
            object.__setattr__(self, "status", MemoryStatus(str(self.status)))

        if not str(self.topic).strip():
            raise KnowledgeError("A memory needs a topic")
        if not str(self.content).strip():
            raise KnowledgeError("A memory needs content")
        if not str(self.source).strip():
            raise KnowledgeError("A memory needs a source: provenance is not optional")
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise KnowledgeError(f"Confidence must be between 0 and 1, got {self.confidence}")

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "topic": self.topic,
            "status": self.status.value,
            "content": self.content,
            "data_json": self.data,
            "confidence": self.confidence,
            "source": self.source,
            "source_ref": self.source_ref,
            "occurred_at": self.occurred_at,
            "created_at": self.created_at,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "scope": self.scope or "global",
        }
