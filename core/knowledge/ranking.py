from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.knowledge.models import MemoryRecord, MemoryStatus


#: Words carrying no signal in a record. Deliberately short: this content is
#: terse and technical, so aggressive stopword removal costs more than it saves.
_STOPWORDS = frozenset(
    """a an and are as at be by for from had has have in into is it its of on
    or that the to was were with""".split()
)

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?")

#: How much a record's own standing counts, before any text match. A rule we
#: accepted should outrank an idea we have not tested, all else equal.
_STATUS_PRIOR: dict[MemoryStatus, float] = {
    MemoryStatus.ACCEPTED: 0.20,
    MemoryStatus.SUPPORTED: 0.15,
    MemoryStatus.OBSERVED: 0.10,
    MemoryStatus.TESTING: 0.08,
    MemoryStatus.PROPOSED: 0.05,
    MemoryStatus.REJECTED: 0.0,
    MemoryStatus.SUPERSEDED: 0.0,
}


def tokenize(text: str) -> set[str]:
    """Words and numbers, lowercased.

    Numbers are kept: "RSI 47" and "2.1x" are the content in an observation,
    not noise. Identifiers keep their shape so a symbol stays one token.
    """
    return {
        token.lower()
        for token in _TOKEN.findall(text or "")
        if token.lower() not in _STOPWORDS and len(token) > 1
    }


def record_tokens(record: MemoryRecord) -> set[str]:
    """Everything about a record worth matching on, content and structured data."""
    tokens = tokenize(record.content)
    tokens |= tokenize(record.topic.replace("/", " "))
    for key, value in record.data.items():
        tokens |= tokenize(str(key))
        tokens |= tokenize(str(value))
    return tokens


def overlap(query_tokens: set[str], target_tokens: set[str]) -> float:
    """Share of the query found in the target, in 0..1.

    Coverage of the query rather than Jaccard: a long record should not be
    penalised for containing more than was asked about.
    """
    if not query_tokens or not target_tokens:
        return 0.0
    return len(query_tokens & target_tokens) / len(query_tokens)


def recency_weight(record: MemoryRecord, *, now: datetime | None = None, half_life_days: float = 30.0) -> float:
    """Newer records count for more, decaying smoothly rather than by cliff."""
    stamp = record.occurred_at or record.created_at
    moment = _parse(stamp)
    if moment is None:
        return 0.0
    reference = now or datetime.now(timezone.utc)
    age_days = max(0.0, (reference - moment).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(0.5, half_life_days))


@dataclass(frozen=True)
class ScoredRecord:
    record: MemoryRecord
    score: float
    reasons: dict[str, float] = field(default_factory=dict)


def score(
    record: MemoryRecord,
    query_tokens: set[str],
    *,
    now: datetime | None = None,
    text_weight: float = 1.0,
    recency_weight_factor: float = 0.25,
) -> ScoredRecord:
    """Score one record, keeping the parts so a ranking can be explained.

    The weights are a starting point, not a tuned model. They are exposed as
    arguments so they can be moved on evidence from real retrievals rather
    than by editing constants.
    """
    text = overlap(query_tokens, record_tokens(record)) if query_tokens else 0.0
    recency = recency_weight(record, now=now)
    prior = _STATUS_PRIOR.get(record.status, 0.0)
    reasons = {
        "text": round(text * text_weight, 4),
        "recency": round(recency * recency_weight_factor, 4),
        "status": round(prior, 4),
    }
    return ScoredRecord(record=record, score=round(sum(reasons.values()), 4), reasons=reasons)


def estimate_tokens(text: str) -> int:
    """Rough token count. Four characters per token is close enough for a budget."""
    return max(1, len(text or "") // 4)


def fit_to_budget(scored: list[ScoredRecord], max_tokens: int | None) -> tuple[list[ScoredRecord], int]:
    """Take records in order until the budget runs out.

    Retrieval that overflows the prompt is worse than retrieval that returns
    less, so this truncates rather than summarising.
    """
    if max_tokens is None:
        return list(scored), sum(estimate_tokens(item.record.content) for item in scored)
    kept: list[ScoredRecord] = []
    spent = 0
    for item in scored:
        cost = estimate_tokens(item.record.content)
        if spent + cost > max_tokens:
            break
        kept.append(item)
        spent += cost
    return kept, spent


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


__all__ = [
    "ScoredRecord",
    "estimate_tokens",
    "fit_to_budget",
    "overlap",
    "record_tokens",
    "recency_weight",
    "score",
    "tokenize",
]
