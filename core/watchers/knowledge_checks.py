# File: core/watchers/knowledge_checks.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.knowledge.models import MemoryKind, MemoryRecord
from core.watchers.checks import CheckKind, CheckResult

ASSESSMENT_SOURCE_PREFIX = "iris:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _moment(record: MemoryRecord) -> datetime | None:
    for value in (record.occurred_at, record.created_at):
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _age_hours(record: MemoryRecord, now: datetime) -> float | None:
    moment = _moment(record)
    return None if moment is None else (now - moment).total_seconds() / 3600.0


def _graph(context: Any) -> Any:
    graph = getattr(context, "knowledge", None) if context is not None else None
    if graph is None:
        raise ValueError("This watcher needs the knowledge graph, and none is attached")
    return graph


def _topic(params: dict[str, Any]) -> str:
    topic = str(params.get("topic") or "").strip().lower()
    if not topic:
        raise ValueError("This watcher needs topic=<topic>")
    return topic


def _describe_age(hours: float | None) -> str:
    if hours is None:
        return "an unknown time ago"
    if hours < 1:
        return f"{hours * 60:.0f} min ago"
    if hours < 48:
        return f"{hours:.1f} h ago"
    return f"{hours / 24:.1f} d ago"


def no_new_records(params: dict[str, Any], _baseline: Any, context: Any) -> CheckResult:
    graph = _graph(context)
    topic = _topic(params)
    hours = float(params.get("hours", 24))
    kind = MemoryKind(str(params.get("kind") or MemoryKind.OBSERVATION.value))
    newest = graph.records.list_by_topic(topic, kind=kind, limit=1)
    now = _now()
    if not newest:
        return CheckResult(True, f"No {kind.value} has ever been recorded under {topic}", None)
    age = _age_hours(newest[0], now)
    triggered = age is None or age > hours
    return CheckResult(
        triggered,
        f"Newest {kind.value} under {topic} arrived {_describe_age(age)}; expected one every {hours:g} h",
        newest[0].id,
    )


def outcomes_overdue(params: dict[str, Any], _baseline: Any, context: Any) -> CheckResult:
    graph = _graph(context)
    topic = _topic(params)
    hours = float(params.get("hours", 24))
    minimum = int(params.get("min_count", 1))
    now = _now()
    open_records = graph.open_observations(topic, limit=int(params.get("limit", 200)))
    overdue = [item for item in open_records if (_age_hours(item, now) or 0.0) > hours]
    oldest = max((_age_hours(item, now) or 0.0) for item in overdue) if overdue else 0.0
    return CheckResult(
        len(overdue) >= minimum,
        f"{len(overdue)} observation(s) under {topic} have waited more than {hours:g} h for an outcome"
        + (f"; the oldest is {_describe_age(oldest)}" if overdue else ""),
        len(overdue),
        clear_when=lambda value: int(value) == 0,
    )


def no_assessments(params: dict[str, Any], _baseline: Any, context: Any) -> CheckResult:
    graph = _graph(context)
    topic = _topic(params)
    hours = float(params.get("hours", 24))
    prefix = str(params.get("source_prefix") or ASSESSMENT_SOURCE_PREFIX)
    now = _now()
    decisions = graph.records.list_by_topic(topic, kind=MemoryKind.DECISION, limit=int(params.get("limit", 50)))
    ours = [item for item in decisions if str(item.source).startswith(prefix)]
    if not ours:
        return CheckResult(True, f"Iris has never scored anything under {topic}", None)
    age = _age_hours(ours[0], now)
    triggered = age is None or age > hours
    return CheckResult(
        triggered,
        f"Iris last scored {topic} {_describe_age(age)}; expected an assessment every {hours:g} h",
        ours[0].id,
    )


KNOWLEDGE_KINDS: dict[str, CheckKind] = {
    kind.name: kind
    for kind in (
        CheckKind(
            "no_new_records",
            "A topic stops receiving observations: a candidate list that stopped arriving",
            {"topic": "topic, e.g. trading/candidates", "hours": "expected at least this often (default 24)", "kind": "record kind (default observation)"},
            no_new_records,
            needs_context=True,
        ),
        CheckKind(
            "outcomes_overdue",
            "Observations sit open without an outcome for too long",
            {"topic": "topic", "hours": "how long is too long (default 24)", "min_count": "at least this many (default 1)"},
            outcomes_overdue,
            needs_context=True,
        ),
        CheckKind(
            "no_assessments",
            "Iris stops scoring a topic it is supposed to be scoring",
            {"topic": "topic", "hours": "expected at least this often (default 24)", "source_prefix": "source prefix (default iris:)"},
            no_assessments,
            needs_context=True,
        ),
    )
}
