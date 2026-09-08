# File: core/knowledge/appraisal.py

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

from core.knowledge.graph import KnowledgeGraph
from core.knowledge.links import MemoryLink, MemoryRelation
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.retrieval import KnowledgeQuery, KnowledgeRetriever

CONTRACT = "shadow-2"

FAVOURABLE_KEY = "favourable"

METHOD_FEATURES = "feature-knn"

METHOD_TEXT = "text-similarity"

POOL_LIMIT = 5000


class Basis(str, Enum):
    NONE = "none"
    OBSERVATIONS = "observations"
    HYPOTHESIS = "hypothesis"


@dataclass(frozen=True)
class AppliedRule:
    hypothesis_id: str
    content: str
    supporting: int
    contradicting: int
    matched_on: tuple[str, ...]


@dataclass(frozen=True)
class _Rule:
    record: MemoryRecord
    region: dict[str, tuple[float, float]]
    supporting: int
    contradicting: int

    def applies_to(self, features: dict[str, float]) -> tuple[str, ...] | None:
        shared = [name for name in self.region if name in features]
        if not shared:
            return None
        for name in shared:
            low, high = self.region[name]
            if not low <= features[name] <= high:
                return None
        return tuple(sorted(shared))


@dataclass(frozen=True)
class Appraisal:
    record_id: str
    basis: Basis
    sample: int
    favourable: int
    unfavourable: int
    score: float | None
    rationale: str
    rank: int | None = None
    topic: str = ""
    source_ref: str | None = None
    method: str = METHOD_FEATURES
    rules: tuple[AppliedRule, ...] = ()

    @property
    def binding(self) -> bool:
        return False


@dataclass(frozen=True)
class _Pool:
    rows: list[tuple[str, dict[str, float], bool]] = field(default_factory=list)
    scales: dict[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.rows)


def numeric_features(data: dict) -> dict[str, float]:
    found: dict[str, float] = {}
    for key, value in (data or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        found[str(key)] = float(value)
    return found


class Appraiser:
    def __init__(
        self,
        graph: KnowledgeGraph,
        retriever: KnowledgeRetriever,
        *,
        minimum_evidence: int = 5,
        neighbours: int = 50,
    ) -> None:
        self.graph = graph
        self.retriever = retriever
        self.minimum_evidence = max(1, int(minimum_evidence))
        self.neighbours = max(1, int(neighbours))

    def appraise_many(
        self, record_ids: Sequence[str], *, record: bool = False
    ) -> list[Appraisal]:
        records = []
        for record_id in record_ids:
            found = self.graph.records.get(record_id)
            if found is None:
                raise KnowledgeError(f"No such memory: {record_id}")
            records.append(found)

        boundaries: dict[tuple[str, str | None], datetime | None] = {}
        for item in records:
            group = (item.topic, item.source_ref)
            seen = _moment(item.occurred_at)
            if group not in boundaries:
                boundaries[group] = seen
            elif seen is not None and (boundaries[group] is None or seen < boundaries[group]):
                boundaries[group] = seen

        pools: dict[tuple[str, str | None], _Pool] = {}
        rules: dict[str, list[_Rule]] = {}
        for item in records:
            group = (item.topic, item.source_ref)
            if group not in pools:
                pools[group] = self._pool(item.topic, item.source_ref, boundaries[group])
            if item.topic not in rules:
                rules[item.topic] = self._rules(item.topic)

        appraisals = _ranked(
            [
                self._appraise(
                    item,
                    pools[(item.topic, item.source_ref)],
                    rules[item.topic],
                    boundaries[(item.topic, item.source_ref)],
                )
                for item in records
            ]
        )
        if record:
            self.record(appraisals)
        return appraisals

    def record(self, appraisals: Sequence[Appraisal]) -> list[MemoryRecord]:
        wanted = [item for item in appraisals if item.topic]
        if not wanted:
            return []
        decisions = [
            MemoryRecord(
                kind=MemoryKind.DECISION,
                topic=item.topic,
                content=item.rationale,
                source=f"iris:{CONTRACT}",
                source_ref=item.source_ref,
                confidence=item.score,
                data={
                    "contract": CONTRACT,
                    "binding": False,
                    "method": item.method,
                    "basis": item.basis.value,
                    "rank": item.rank,
                    "sample": item.sample,
                    "favourable": item.favourable,
                    "unfavourable": item.unfavourable,
                },
            )
            for item in wanted
        ]
        self.graph.records.add_many(decisions)
        self.graph.links.add_many(
            [
                MemoryLink(
                    source_id=decision.id,
                    target_id=item.record_id,
                    relation=MemoryRelation.DECIDED_FROM,
                )
                for decision, item in zip(decisions, wanted)
            ]
        )
        return decisions

    def _pool(
        self, topic: str, cohort: str | None = None, before: datetime | None = None
    ) -> _Pool:
        rows: list[tuple[str, dict[str, float], bool]] = []
        parameters: list[object] = [
            MemoryRelation.OUTCOME_OF.value,
            topic,
            MemoryKind.OBSERVATION.value,
        ]
        withheld = ""
        if cohort:
            withheld = "AND (m.source_ref IS NULL OR m.source_ref <> ?) "
            parameters.append(cohort)
        parameters.append(POOL_LIMIT)

        with self.graph.records.database.connect() as conn:
            found = conn.execute(
                "SELECT m.id, m.data_json, m.occurred_at, o.data_json AS outcome_json "
                "FROM memories m "
                "JOIN memory_links l ON l.target_id = m.id AND l.relation = ? "
                "JOIN memories o ON o.id = l.source_id "
                "WHERE m.topic = ? AND m.kind = ? " + withheld + "ORDER BY m.sequence DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()

        for row in found:
            verdict = _json(row["outcome_json"]).get(FAVOURABLE_KEY)
            if not isinstance(verdict, bool):
                continue
            if not _precedes(row["occurred_at"], before):
                continue
            rows.append((str(row["id"]), numeric_features(_json(row["data_json"])), verdict))

        return _Pool(rows=rows, scales=_scales(rows))

    def _rules(self, topic: str) -> list[_Rule]:
        found: list[_Rule] = []
        for record in self.graph.records.list_by_topic(
            topic, kind=MemoryKind.HYPOTHESIS, limit=200
        ):
            if record.status is not MemoryStatus.ACCEPTED:
                continue
            evidence = self.graph.evidence_for(record.id)
            region = _region(self._observations_behind([item for item, _ in evidence.supporting]))
            if not region:
                continue
            found.append(
                _Rule(
                    record=record,
                    region=region,
                    supporting=len(evidence.supporting),
                    contradicting=len(evidence.contradicting),
                )
            )
        return found

    def _observations_behind(self, records: Sequence[MemoryRecord]) -> list[MemoryRecord]:
        found: list[MemoryRecord] = []
        for record in records:
            if record.kind is MemoryKind.OBSERVATION:
                found.append(record)
                continue
            for link in self.graph.links.links_from(record.id, MemoryRelation.OUTCOME_OF):
                behind = self.graph.records.get(link.target_id)
                if behind is not None and behind.kind is MemoryKind.OBSERVATION:
                    found.append(behind)
        return found

    def _applied(self, rules: Sequence[_Rule], features: dict[str, float]) -> tuple[AppliedRule, ...]:
        applied: list[AppliedRule] = []
        for rule in rules:
            matched = rule.applies_to(features)
            if matched is None:
                continue
            applied.append(
                AppliedRule(
                    hypothesis_id=rule.record.id,
                    content=rule.record.content,
                    supporting=rule.supporting,
                    contradicting=rule.contradicting,
                    matched_on=matched,
                )
            )
        return tuple(applied)

    def _appraise(
        self,
        record: MemoryRecord,
        pool: _Pool,
        rules: Sequence[_Rule],
        before: datetime | None = None,
    ) -> Appraisal:
        features = numeric_features(record.data)
        applied = self._applied(rules, features) if features else ()
        shared = [name for name in features if name in pool.scales]
        if pool and shared:
            base = self._by_features(record, pool, features, shared)
        else:
            base = self._by_text(record, before)
        return _with_rules(base, applied)

    def _by_features(
        self, record: MemoryRecord, pool: _Pool, features: dict[str, float], shared: list[str]
    ) -> Appraisal:
        distances: list[tuple[float, bool]] = []
        for other_id, other, verdict in pool.rows:
            if other_id == record.id:
                continue
            overlap = [name for name in shared if name in other]
            if not overlap:
                continue
            distance = sum(
                abs(features[name] - other[name]) / pool.scales[name] for name in overlap
            ) / len(overlap)
            distances.append((distance, verdict))

        if not distances:
            return self._by_text(record)

        distances.sort(key=lambda pair: pair[0])
        nearest = distances[: self.neighbours]
        favourable = sum(1 for _, verdict in nearest if verdict)
        unfavourable = len(nearest) - favourable

        if len(nearest) < self.minimum_evidence:
            return Appraisal(
                record_id=record.id,
                basis=Basis.OBSERVATIONS,
                sample=len(nearest),
                favourable=favourable,
                unfavourable=unfavourable,
                score=None,
                rationale=(
                    f"{len(nearest)} of {self.minimum_evidence} resolved neighbours needed before "
                    f"a rate means anything ({favourable} favourable, {unfavourable} not)."
                ),
                topic=record.topic,
                source_ref=record.source_ref,
            )

        weights = [1.0 / (1.0 + distance) for distance, _ in nearest]
        total = sum(weights)
        score = sum(
            weight for weight, (_, verdict) in zip(weights, nearest) if verdict
        ) / total
        span = ", ".join(sorted(shared))
        return Appraisal(
            record_id=record.id,
            basis=Basis.OBSERVATIONS,
            sample=len(nearest),
            favourable=favourable,
            unfavourable=unfavourable,
            score=round(score, 4),
            rationale=(
                f"{favourable} of {len(nearest)} nearest observations by {span} were favourable. "
                "The rate leans on the closest of them. An observed rate, not a prediction."
            ),
            topic=record.topic,
            source_ref=record.source_ref,
        )

    def _by_text(self, record: MemoryRecord, before: datetime | None = None) -> Appraisal:
        similar = self._similar(record, before)
        if not similar:
            return Appraisal(
                record_id=record.id,
                basis=Basis.NONE,
                sample=0,
                favourable=0,
                unfavourable=0,
                score=None,
                rationale=_nothing_comparable(record, before),
                topic=record.topic,
                source_ref=record.source_ref,
                method=METHOD_TEXT,
            )

        outcomes = self.graph.outcomes_for([item.id for item in similar])
        favourable = 0
        unfavourable = 0
        unlabelled = 0
        for outcome in outcomes.values():
            verdict = outcome.data.get(FAVOURABLE_KEY)
            if verdict is True:
                favourable += 1
            elif verdict is False:
                unfavourable += 1
            else:
                unlabelled += 1

        sample = favourable + unfavourable
        if sample == 0:
            return Appraisal(
                record_id=record.id,
                basis=Basis.NONE,
                sample=0,
                favourable=0,
                unfavourable=0,
                score=None,
                rationale=_nothing_to_go_on(len(similar), len(outcomes), unlabelled),
                topic=record.topic,
                source_ref=record.source_ref,
                method=METHOD_TEXT,
            )

        if sample < self.minimum_evidence:
            return Appraisal(
                record_id=record.id,
                basis=Basis.OBSERVATIONS,
                sample=sample,
                favourable=favourable,
                unfavourable=unfavourable,
                score=None,
                rationale=(
                    f"{sample} of {self.minimum_evidence} resolved outcomes needed before a rate "
                    f"means anything ({favourable} favourable, {unfavourable} not)."
                ),
                topic=record.topic,
                source_ref=record.source_ref,
                method=METHOD_TEXT,
            )

        return Appraisal(
            record_id=record.id,
            basis=Basis.OBSERVATIONS,
            sample=sample,
            favourable=favourable,
            unfavourable=unfavourable,
            score=round(favourable / sample, 4),
            rationale=(
                f"{favourable} of {sample} resolved outcomes on textually comparable observations "
                "were favourable. An observed rate, not a prediction."
            ),
            topic=record.topic,
            source_ref=record.source_ref,
            method=METHOD_TEXT,
        )

    def _similar(
        self, record: MemoryRecord, before: datetime | None = None
    ) -> list[MemoryRecord]:
        result = self.retriever.retrieve(
            KnowledgeQuery(
                text=record.content,
                topic=record.topic,
                kinds=(MemoryKind.OBSERVATION,),
                limit=self.neighbours,
                candidate_limit=max(self.neighbours * 4, 200),
            )
        )
        return [
            item.record
            for item in result.records
            if item.record.id != record.id
            and not _same_cohort(record, item.record)
            and _precedes(item.record.occurred_at, before)
        ]


def _json(raw: object) -> dict:
    try:
        loaded = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _scales(rows: list[tuple[str, dict[str, float], bool]]) -> dict[str, float]:
    columns: dict[str, list[float]] = {}
    for _, features, _ in rows:
        for name, value in features.items():
            columns.setdefault(name, []).append(value)

    scales: dict[str, float] = {}
    for name, values in columns.items():
        if len(values) < 2:
            continue
        values.sort()
        low = values[len(values) // 4]
        high = values[(len(values) * 3) // 4]
        spread = high - low
        if spread <= 0:
            spread = (values[-1] - values[0]) or 1.0
        scales[name] = abs(spread)
    return scales


def _region(observations: Sequence[MemoryRecord]) -> dict[str, tuple[float, float]]:
    columns: dict[str, list[float]] = {}
    for record in observations:
        for name, value in numeric_features(record.data).items():
            columns.setdefault(name, []).append(value)
    return {
        name: (min(values), max(values))
        for name, values in columns.items()
        if len(values) >= 2 and max(values) > min(values)
    }


def _with_rules(appraisal: Appraisal, applied: tuple[AppliedRule, ...]) -> Appraisal:
    if not applied:
        return appraisal
    names = "; ".join(item.content.rstrip(".") for item in applied)
    return Appraisal(
        record_id=appraisal.record_id,
        basis=Basis.HYPOTHESIS,
        sample=appraisal.sample,
        favourable=appraisal.favourable,
        unfavourable=appraisal.unfavourable,
        score=appraisal.score,
        rationale=(
            f"{appraisal.rationale} Covered by an approved rule: {names}. "
            "The rate above is still counted from outcomes, not from the rule."
        ),
        rank=appraisal.rank,
        topic=appraisal.topic,
        source_ref=appraisal.source_ref,
        method=appraisal.method,
        rules=applied,
    )


def _same_cohort(record: MemoryRecord, other: MemoryRecord) -> bool:
    return bool(record.source_ref) and other.source_ref == record.source_ref


def _moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _precedes(occurred_at: str | None, before: datetime | None) -> bool:
    if before is None:
        return True
    seen = _moment(occurred_at)
    if seen is None:
        return False
    if (seen.tzinfo is None) != (before.tzinfo is None):
        return False
    return seen < before


def _nothing_comparable(record: MemoryRecord, before: datetime | None = None) -> str:
    if before is not None:
        return (
            f"Nothing recorded before {before.isoformat()} resembles this. A cohort cannot score "
            "itself, and nothing later can score it either, so there is no basis for this one yet."
        )
    if record.source_ref:
        return (
            f"Nothing outside cohort {record.source_ref} resembles this. A cohort cannot score "
            "itself, so there is no basis for this one yet."
        )
    return "Nothing resembling this has been seen before."


def _nothing_to_go_on(similar: int, closed: int, unlabelled: int) -> str:
    if closed == 0:
        return f"{similar} comparable observations, none of them closed by an outcome yet."
    return (
        f"{similar} comparable observations and {closed} outcomes, but {unlabelled} of those "
        "carry no favourable flag, so there is nothing to count."
    )


def _ranked(appraisals: list[Appraisal]) -> list[Appraisal]:
    scored = sorted(
        [item for item in appraisals if item.score is not None],
        key=lambda item: (-item.score, -item.sample, item.record_id),
    )
    positions = {item.record_id: index + 1 for index, item in enumerate(scored)}
    return [
        Appraisal(
            record_id=item.record_id,
            basis=item.basis,
            sample=item.sample,
            favourable=item.favourable,
            unfavourable=item.unfavourable,
            score=item.score,
            rationale=item.rationale,
            rank=positions.get(item.record_id),
            topic=item.topic,
            source_ref=item.source_ref,
            method=item.method,
            rules=item.rules,
        )
        for item in appraisals
    ]


__all__ = [
    "AppliedRule",
    "Appraisal",
    "Appraiser",
    "Basis",
    "CONTRACT",
    "FAVOURABLE_KEY",
    "METHOD_FEATURES",
    "METHOD_TEXT",
    "numeric_features",
]
