# File: core/knowledge/retrieval.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.knowledge.embeddings import MemoryEmbeddingIndex
from core.knowledge.models import MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.ranking import ScoredRecord, fit_to_budget, score, tokenize
from core.knowledge.repository import MEMORY_COLUMNS, record_from_row
from core.knowledge.schema import ensure_schema
from core.storage.sqlite_database import SQLiteDatabase


_RANGE_END = "￿"


@dataclass(frozen=True)
class KnowledgeQuery:

    text: str = ""
    topic: str | None = None
    topic_prefix: str | None = None
    kinds: tuple[MemoryKind, ...] = ()
    statuses: tuple[MemoryStatus, ...] = ()
    occurred_after: str | None = None
    occurred_before: str | None = None
    include_superseded: bool = False
    candidate_limit: int = 400
    limit: int = 10
    max_tokens: int | None = None
    minimum_text_match: float = 0.01
    scopes: tuple[str, ...] = ()
    scope_weight: float = 0.08


@dataclass(frozen=True)
class RetrievalResult:
    records: list[ScoredRecord] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_records(self) -> list[MemoryRecord]:
        return [item.record for item in self.records]

    def as_context(self) -> str:
        return "\n".join(f"- {item.record.content}" for item in self.records)


class KnowledgeRetriever:

    def __init__(self, database: SQLiteDatabase, embeddings: MemoryEmbeddingIndex | None = None) -> None:
        self.database = database
        self.embeddings = embeddings
        ensure_schema(database)

    def retrieve(self, query: KnowledgeQuery, *, now: datetime | None = None) -> RetrievalResult:
        limit = max(1, int(query.candidate_limit))
        match = _match_expression(query.text)
        semantic: dict[str, float] = {}
        semantic_diagnostics: dict[str, Any] = {}

        if match:
            clauses, params = _filters(query, table="m")
            columns = ", ".join(f"m.{name.strip()}" for name in MEMORY_COLUMNS.split(","))
            sql = (
                f"SELECT {columns} FROM memories_fts f "
                "JOIN memories m ON m.sequence = f.rowid "
                f"WHERE memories_fts MATCH ? AND {' AND '.join(clauses)} "
                "ORDER BY bm25(memories_fts), m.sequence DESC LIMIT ?"
            )
            args: list[Any] = [match, *params, limit]
        else:
            clauses, params = _filters(query)
            sql = (
                f"SELECT {MEMORY_COLUMNS} FROM memories "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, sequence DESC LIMIT ?"
            )
            args = [*params, limit]

        with self.database.connect() as conn:
            rows = conn.execute(sql, args).fetchall()

        candidates = [record_from_row(row) for row in rows]
        if match and self.embeddings is not None and self.embeddings.available:
            semantic, extra, semantic_diagnostics = self._semantic_candidates(query, limit, {item.id for item in candidates})
            candidates.extend(extra)
        query_tokens = tokenize(query.text)
        scored = sorted(
            (
                score(
                    record,
                    query_tokens,
                    now=now,
                    semantic=semantic.get(record.id),
                    scope_bonus=query.scope_weight if query.scopes and record.scope != "global" and record.scope in query.scopes else 0.0,
                )
                for record in candidates
            ),
            key=lambda item: (-item.score, _newest_first(item.record)),
        )
        relevant = scored
        if query_tokens and query.minimum_text_match > 0:
            relevant = [item for item in scored if item.reasons.get("text", 0.0) >= query.minimum_text_match]
        top = relevant[: max(1, int(query.limit))]
        kept, spent = fit_to_budget(top, query.max_tokens)

        return RetrievalResult(
            records=kept,
            diagnostics={
                "candidates_examined": len(candidates),
                "candidate_limit_reached": len(candidates) >= max(1, int(query.candidate_limit)),
                "dropped_as_irrelevant": len(scored) - len(relevant),
                "returned": len(kept),
                "dropped_for_budget": len(top) - len(kept),
                "estimated_tokens": spent,
                "query_tokens": sorted(query_tokens),
                "top_scores": [
                    {"id": item.record.id, "score": item.score, "reasons": item.reasons}
                    for item in kept
                ],
                **semantic_diagnostics,
            },
        )

    def _semantic_candidates(
        self, query: KnowledgeQuery, limit: int, already: set[str]
    ) -> tuple[dict[str, float], list[MemoryRecord], dict[str, Any]]:
        assert self.embeddings is not None
        try:
            hits = self.embeddings.search(query.text, k=min(limit, 200))
        except Exception as error:
            return {}, [], {"semantic_candidates": 0, "semantic_error": str(error)}
        if not hits:
            return {}, [], {"semantic_candidates": 0}
        by_sequence = {sequence: similarity for sequence, similarity in hits}
        clauses, params = _filters(query, table="m")
        placeholders = ", ".join("?" * len(by_sequence))
        columns = ", ".join(f"m.{name.strip()}" for name in MEMORY_COLUMNS.split(","))
        sql = (
            f"SELECT m.sequence AS vec_sequence, {columns} FROM memories m "
            f"WHERE m.sequence IN ({placeholders}) AND {' AND '.join(clauses)}"
        )
        with self.database.connect() as conn:
            rows = conn.execute(sql, [*by_sequence.keys(), *params]).fetchall()
        relevance: dict[str, float] = {}
        extra: list[MemoryRecord] = []
        for row in rows:
            record = record_from_row(row)
            similarity = by_sequence[int(row["vec_sequence"])]
            mapped = self.embeddings.config.relevance(similarity)
            if mapped <= 0.0:
                continue
            relevance[record.id] = mapped
            if record.id not in already:
                extra.append(record)
        return relevance, extra, {
            "semantic_candidates": len(relevance),
            "semantic_added": len(extra),
            "semantic_top_similarity": round(max(by_sequence.values()), 4),
        }

    def similar_to(self, record: MemoryRecord, *, limit: int = 5, **overrides: Any) -> RetrievalResult:
        query = KnowledgeQuery(
            text=record.content,
            topic=record.topic,
            kinds=(record.kind,),
            limit=limit + 1,
            **overrides,
        )
        result = self.retrieve(query)
        kept = [item for item in result.records if item.record.id != record.id][:limit]
        return RetrievalResult(records=kept, diagnostics={**result.diagnostics, "returned": len(kept)})


def _match_expression(text: str) -> str:
    terms = sorted(tokenize(text))
    if not terms:
        return ""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _newest_first(record: MemoryRecord) -> tuple[int, ...]:
    return tuple(-ord(char) for char in record.created_at)


def _filters(query: KnowledgeQuery, *, table: str = "") -> tuple[list[str], list[Any]]:
    at = f"{table}." if table else ""
    clauses: list[str] = []
    params: list[Any] = []

    if query.topic is not None:
        clauses.append(f"{at}topic = ?")
        params.append(query.topic)
    elif query.topic_prefix:
        clauses.append(f"{at}topic >= ? AND {at}topic < ?")
        params.extend([query.topic_prefix, query.topic_prefix + _RANGE_END])

    if query.kinds:
        clauses.append(f"{at}kind IN ({', '.join('?' * len(query.kinds))})")
        params.extend(MemoryKind(kind).value for kind in query.kinds)

    if query.statuses:
        clauses.append(f"{at}status IN ({', '.join('?' * len(query.statuses))})")
        params.extend(MemoryStatus(status).value for status in query.statuses)
    elif not query.include_superseded:
        clauses.append(f"{at}status NOT IN (?, ?)")
        params.extend([MemoryStatus.SUPERSEDED.value, MemoryStatus.RETIRED.value])

    if query.scopes:
        clauses.append(f"{at}scope IN ({', '.join('?' * len(query.scopes))})")
        params.extend(str(scope) for scope in query.scopes)

    if query.occurred_after:
        clauses.append(f"COALESCE({at}occurred_at, {at}created_at) >= ?")
        params.append(query.occurred_after)
    if query.occurred_before:
        clauses.append(f"COALESCE({at}occurred_at, {at}created_at) <= ?")
        params.append(query.occurred_before)

    return clauses or ["1 = 1"], params
