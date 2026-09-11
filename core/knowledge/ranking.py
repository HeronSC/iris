# File: core/knowledge/ranking.py

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.knowledge.models import MemoryRecord, MemoryStatus


_STOPWORDS = frozenset(
    """a an and are as at be been being by can could did do does for from had
    has have he her him his how i in into is it its me my of on or our she
    should some that the their them there these they this those to us was we
    were what when where which who why will with would you your""".split()
)

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?")

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
    return {
        token.lower()
        for token in _TOKEN.findall(text or "")
        if token.lower() not in _STOPWORDS and len(token) > 1
    }


def record_tokens(record: MemoryRecord) -> set[str]:
    tokens = tokenize(record.content)
    tokens |= tokenize(record.topic.replace("/", " "))
    for key, value in record.data.items():
        tokens |= tokenize(str(key))
        tokens |= tokenize(str(value))
    return tokens


def overlap(query_tokens: set[str], target_tokens: set[str]) -> float:
    if not query_tokens or not target_tokens:
        return 0.0
    return len(query_tokens & target_tokens) / len(query_tokens)


def recency_weight(record: MemoryRecord, *, now: datetime | None = None, half_life_days: float = 30.0) -> float:
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
    semantic: float | None = None,
) -> ScoredRecord:
    lexical = overlap(query_tokens, record_tokens(record)) if query_tokens else 0.0
    text = max(lexical, semantic) if semantic is not None else lexical
    recency = recency_weight(record, now=now)
    prior = _STATUS_PRIOR.get(record.status, 0.0)
    reasons = {
        "text": round(text * text_weight, 4),
        "recency": round(recency * recency_weight_factor, 4),
        "status": round(prior, 4),
    }
    total = round(reasons["text"] + reasons["recency"] + reasons["status"], 4)
    if semantic is not None:
        reasons["lexical"] = round(lexical, 4)
        reasons["semantic"] = round(semantic, 4)
    return ScoredRecord(record=record, score=total, reasons=reasons)


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def fit_to_budget(scored: list[ScoredRecord], max_tokens: int | None) -> tuple[list[ScoredRecord], int]:
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
