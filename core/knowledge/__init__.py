from __future__ import annotations

"""Durable, append-only knowledge: what Iris saw, decided, suspects and learned.

Distinct from the two older memory namespaces, which this does not replace:
``core.memory`` holds the assistant profile, and
``core.conversation.persistent_memory`` holds conversation topics.
"""

from core.knowledge.models import (
    DEFAULT_STATUS,
    KnowledgeError,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    utc_now_iso,
)
from core.knowledge.repository import KnowledgeRepository

__all__ = [
    "DEFAULT_STATUS",
    "KnowledgeError",
    "KnowledgeRepository",
    "MemoryKind",
    "MemoryRecord",
    "MemoryStatus",
    "utc_now_iso",
]
