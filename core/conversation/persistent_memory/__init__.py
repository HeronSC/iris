from __future__ import annotations

"""Topic-scoped conversation memory.

Split from a single 2393-line module. The layers only depend downward:
text -> models -> topic_state -> repositories -> services -> service.
"""

from core.conversation.persistent_memory.models import (
    MemoryConfig,
    PreparedMemoryContext,
    TopicCandidate,
    TopicRecord,
    diagnostics_to_text,
)
from core.conversation.persistent_memory.repositories import (
    ConversationRepository,
    MessageRepository,
    TopicRepository,
)
from core.conversation.persistent_memory.service import TopicMemoryService

__all__ = [
    "ConversationRepository",
    "MemoryConfig",
    "MessageRepository",
    "PreparedMemoryContext",
    "TopicCandidate",
    "TopicMemoryService",
    "TopicRecord",
    "TopicRepository",
    "diagnostics_to_text",
]
