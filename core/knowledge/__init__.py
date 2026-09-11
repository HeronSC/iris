# File: core/knowledge/__init__.py

from __future__ import annotations

"""Durable, append-only knowledge: what Iris saw, decided, suspects and learned.

Distinct from the two older memory namespaces, which this does not replace:
``core.profile`` holds the assistant profile, and
``core.conversation.persistent_memory`` holds conversation topics.
"""

from core.knowledge.appraisal import Appraisal, Appraiser, Basis
from core.knowledge.comparison import Comparison, RankerScore, compare_rankers, render_comparison
from core.knowledge.embeddings import EmbeddingConfig, MemoryEmbeddingIndex
from core.knowledge.graph import Evidence, Explanation, KnowledgeGraph, render_explanation
from core.knowledge.hypotheses import Assessment, HypothesisPolicy, HypothesisTracker
from core.knowledge.links import (
    EVIDENCE_RELATIONS,
    GROUNDING_RELATIONS,
    LinkRepository,
    MemoryLink,
    MemoryRelation,
)
from core.knowledge.ranking import ScoredRecord, estimate_tokens, tokenize
from core.knowledge.retrieval import KnowledgeQuery, KnowledgeRetriever, RetrievalResult
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
    "Appraisal",
    "EmbeddingConfig",
    "MemoryEmbeddingIndex",
    "Appraiser",
    "Assessment",
    "Basis",
    "Comparison",
    "RankerScore",
    "compare_rankers",
    "render_comparison",
    "DEFAULT_STATUS",
    "EVIDENCE_RELATIONS",
    "Evidence",
    "Explanation",
    "GROUNDING_RELATIONS",
    "HypothesisPolicy",
    "HypothesisTracker",
    "KnowledgeError",
    "KnowledgeGraph",
    "KnowledgeQuery",
    "KnowledgeRepository",
    "KnowledgeRetriever",
    "LinkRepository",
    "MemoryKind",
    "MemoryLink",
    "MemoryRecord",
    "MemoryRelation",
    "MemoryStatus",
    "RetrievalResult",
    "ScoredRecord",
    "ensure_schema",
    "estimate_tokens",
    "render_explanation",
    "tokenize",
    "utc_now_iso",
]
