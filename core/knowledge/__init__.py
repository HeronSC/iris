from __future__ import annotations

"""Durable, append-only knowledge: what Iris saw, decided, suspects and learned.

Distinct from the two older memory namespaces, which this does not replace:
``core.profile`` holds the assistant profile, and
``core.conversation.persistent_memory`` holds conversation topics.
"""

from core.knowledge.graph import Evidence, Explanation, KnowledgeGraph, render_explanation
from core.knowledge.links import (
    EVIDENCE_RELATIONS,
    GROUNDING_RELATIONS,
    LinkRepository,
    MemoryLink,
    MemoryRelation,
)
from core.knowledge.models import (
    DEFAULT_STATUS,
    KnowledgeError,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    utc_now_iso,
)
from core.knowledge.repository import KnowledgeRepository
from core.knowledge.schema import ensure_schema

__all__ = [
    "DEFAULT_STATUS",
    "EVIDENCE_RELATIONS",
    "GROUNDING_RELATIONS",
    "Evidence",
    "Explanation",
    "KnowledgeError",
    "KnowledgeGraph",
    "KnowledgeRepository",
    "LinkRepository",
    "MemoryKind",
    "MemoryLink",
    "MemoryRecord",
    "MemoryRelation",
    "MemoryStatus",
    "ensure_schema",
    "render_explanation",
    "utc_now_iso",
]
