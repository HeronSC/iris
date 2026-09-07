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

    Retrieval runs in two stages. When ``text`` is given, SQLite's own search
    index picks the candidates by relevance and BM25 orders them;
    ``candidate_limit`` bounds how many reach Python, where the scorer re-ranks
    them on recency and standing, which the index knows nothing about. With no
    ``text`` it is a plain filtered browse, newest first.

    Cost follows how many records the query matches rather than how many exist.
    A question with any distinguishing term is a few milliseconds at 200,000
    records; one whose every term appears in every record is the worst case,
    since BM25 must then score them all: about 140ms at 200,000, 6ms at 2,000.
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
    #: Least text overlap a record may have and still be returned, applied only
    #: when ``text`` is given. Without it, recency and standing alone keep a
    #: record with nothing to do with the question, and anything handed to a
    #: model as context is read as relevant to it. Set to 0.0 to keep everything
    #: the filters matched, which is what a browse wants and a question does not.
    minimum_text_match: float = 0.01


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
        limit = max(1, int(query.candidate_limit))
        match = _match_expression(query.text)

        if match:
            # Candidates chosen by relevance, using SQLite's own index and BM25.
            # Selecting them by recency instead left a strong older match
            # unreachable no matter how well it matched.
            clauses, params = _filters(query, table="m")
            columns = ", ".join(f"m.{name.strip()}" for name in MEMORY_COLUMNS.split(","))
            sql = (
                f"SELECT {columns} FROM memories_fts f "
                "JOIN memories m ON m.sequence = f.rowid "
                f"WHERE memories_fts MATCH ? AND {' AND '.join(clauses)} "
                "ORDER BY bm25(memories_fts) LIMIT ?"
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
        query_tokens = tokenize(query.text)
        scored = sorted(
            (score(record, query_tokens, now=now) for record in candidates),
            key=lambda item: (-item.score, item.record.created_at),
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


def _match_expression(text: str) -> str:
    """Turn a person's words into an FTS5 query that cannot be a syntax error.

    Query text arrives as prose and FTS5 gives -, *, ", : and the bare words
    AND, OR, NOT and NEAR their own meanings, so a question like
    "what about the +3.4% result?" would not parse. Each term is quoted
    instead, and they are joined with OR: matching broadly is right here,
    because BM25 orders the candidates and the scorer then applies
    minimum_text_match to drop the weak ones.

    Terms come from the same tokenizer the scorer uses, so the filler in
    "what did we see about volume" is dropped. That matters for more than
    tidiness: a match is only as narrow as its commonest term, and the cost of
    ranking grows with how many records match.
    """
    terms = sorted(tokenize(text))
    if not terms:
        return ""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _filters(query: KnowledgeQuery, *, table: str = "") -> tuple[list[str], list[Any]]:
    """The structured filters, with every column qualified for the caller's FROM.

    ``table`` matters rather than being cosmetic: the search join brings a
    second ``topic`` column into scope, so an unqualified name there is
    ambiguous rather than merely untidy.
    """
    at = f"{table}." if table else ""
    clauses: list[str] = []
    params: list[Any] = []

    if query.topic is not None:
        clauses.append(f"{at}topic = ?")
        params.append(query.topic)
    elif query.topic_prefix:
        # A range rather than LIKE: LIKE is case-insensitive by default, which
        # stops SQLite using the index on topic.
        clauses.append(f"{at}topic >= ? AND {at}topic < ?")
        params.extend([query.topic_prefix, query.topic_prefix + _RANGE_END])

    if query.kinds:
        clauses.append(f"{at}kind IN ({', '.join('?' * len(query.kinds))})")
        params.extend(MemoryKind(kind).value for kind in query.kinds)

    if query.statuses:
        clauses.append(f"{at}status IN ({', '.join('?' * len(query.statuses))})")
        params.extend(MemoryStatus(status).value for status in query.statuses)
    elif not query.include_superseded:
        clauses.append(f"{at}status != ?")
        params.append(MemoryStatus.SUPERSEDED.value)

    if query.occurred_after:
        clauses.append(f"COALESCE({at}occurred_at, {at}created_at) >= ?")
        params.append(query.occurred_after)
    if query.occurred_before:
        clauses.append(f"COALESCE({at}occurred_at, {at}created_at) <= ?")
        params.append(query.occurred_before)

    return clauses or ["1 = 1"], params
