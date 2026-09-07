from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.knowledge.models import MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.ranking import ScoredRecord, fit_to_budget, score, tokenize
from core.knowledge.repository import MEMORY_COLUMNS, record_from_row
from core.knowledge.schema import ensure_schema
from core.storage.sqlite_database import SQLiteDatabase


#: Upper bound for a text range scan. Above every ordinary character, so
#: "trading/" .. "trading/￿" spans exactly the topics beneath a prefix.
_RANGE_END = "￿"


@dataclass(frozen=True)
class KnowledgeQuery:
    """What to look for, and how much of it to bring back.

    The two limits are the point. ``candidate_limit`` bounds what SQL returns,
    and only that bounded set is ranked in Python, so retrieval cost does not
    follow the size of the store: measured at 200,000 records, a default query
    costs about 4ms.

    The trade is real and worth knowing. Candidates are taken newest-first, so
    a strong match older than the window is never ranked at all. Narrow the
    filters, widen ``candidate_limit``, or watch ``candidate_limit_reached`` in
    the diagnostics, which is the only signal that this may have happened.
    Making candidate selection relevance-aware rather than recency-aware needs
    a text index, and that decision should wait for evidence from real use.
    """

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


@dataclass(frozen=True)
class RetrievalResult:
    records: list[ScoredRecord] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_records(self) -> list[MemoryRecord]:
        return [item.record for item in self.records]

    def as_context(self) -> str:
        """The retrieved records as prompt text, most relevant first."""
        return "\n".join(f"- {item.record.content}" for item in self.records)


class KnowledgeRetriever:
    """Structured filtering in SQL, relevance ranking over what comes back.

    Nothing here decides what is worth retrieving on the model's behalf: the
    caller states the filters. What it does guarantee is that the cost is
    bounded and that every result carries why it was chosen.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        ensure_schema(database)

    def retrieve(self, query: KnowledgeQuery, *, now: datetime | None = None) -> RetrievalResult:
        clauses, params = _filters(query)
        params.append(max(1, int(query.candidate_limit)))
        sql = (
            f"SELECT {MEMORY_COLUMNS} FROM memories "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, sequence DESC LIMIT ?"
        )
        with self.database.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        candidates = [record_from_row(row) for row in rows]
        query_tokens = tokenize(query.text)
        scored = sorted(
            (score(record, query_tokens, now=now) for record in candidates),
            key=lambda item: (-item.score, item.record.created_at),
        )
        top = scored[: max(1, int(query.limit))]
        kept, spent = fit_to_budget(top, query.max_tokens)

        return RetrievalResult(
            records=kept,
            diagnostics={
                "candidates_examined": len(candidates),
                "candidate_limit_reached": len(candidates) >= max(1, int(query.candidate_limit)),
                "returned": len(kept),
                "dropped_for_budget": len(top) - len(kept),
                "estimated_tokens": spent,
                "query_tokens": sorted(query_tokens),
                "top_scores": [
                    {"id": item.record.id, "score": item.score, "reasons": item.reasons}
                    for item in kept
                ],
            },
        )

    def similar_to(self, record: MemoryRecord, *, limit: int = 5, **overrides: Any) -> RetrievalResult:
        """Records resembling this one, in its own topic and of its own kind.

        Answers "what previous observations resemble this one" from the design
        document. The record itself is excluded from its own results.
        """
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


def _filters(query: KnowledgeQuery) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if query.topic is not None:
        clauses.append("topic = ?")
        params.append(query.topic)
    elif query.topic_prefix:
        # A range rather than LIKE: LIKE is case-insensitive by default, which
        # stops SQLite using the index on topic.
        clauses.append("topic >= ? AND topic < ?")
        params.extend([query.topic_prefix, query.topic_prefix + _RANGE_END])

    if query.kinds:
        clauses.append(f"kind IN ({', '.join('?' * len(query.kinds))})")
        params.extend(MemoryKind(kind).value for kind in query.kinds)

    if query.statuses:
        clauses.append(f"status IN ({', '.join('?' * len(query.statuses))})")
        params.extend(MemoryStatus(status).value for status in query.statuses)
    elif not query.include_superseded:
        clauses.append("status != ?")
        params.append(MemoryStatus.SUPERSEDED.value)

    if query.occurred_after:
        clauses.append("COALESCE(occurred_at, created_at) >= ?")
        params.append(query.occurred_after)
    if query.occurred_before:
        clauses.append("COALESCE(occurred_at, created_at) <= ?")
        params.append(query.occurred_before)

    return clauses or ["1 = 1"], params
