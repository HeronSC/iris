# File: core/knowledge/appraisal.py

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy

from core.knowledge.graph import KnowledgeGraph
from core.knowledge.links import MemoryLink, MemoryRelation
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.retrieval import KnowledgeQuery, KnowledgeRetriever

CONTRACT = "shadow-2"

FAVOURABLE_KEY = "favourable"

METHOD_FEATURES = "feature-knn"

METHOD_TEXT = "text-similarity"

POOL_LIMIT = 250000


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
    base_rate: float | None = None

    @property
    def binding(self) -> bool:
        return False


@dataclass
class _Pool:
    rows: list[tuple[str, dict[str, float], bool]] = field(default_factory=list)
    scales: dict[str, float] = field(default_factory=dict)
    base_rate: float | None = None
    symbols: list[str | None] = field(default_factory=list)
    columns: dict[str, int] = field(default_factory=dict, repr=False)
    positions: dict[str, int] = field(default_factory=dict, repr=False)
    matrix: Any = field(default=None, repr=False)
    verdicts: Any = field(default=None, repr=False)

    def __bool__(self) -> bool:
        return bool(self.rows)

    def prepare(self) -> None:
        if self.matrix is not None:
            return
        names = sorted(self.scales)
        self.columns = {name: index for index, name in enumerate(names)}
        table = numpy.full((len(self.rows), len(names)), numpy.nan)
        for row_index, (record_id, features, _) in enumerate(self.rows):
            self.positions[record_id] = row_index
            for name, value in features.items():
                column = self.columns.get(name)
                if column is not None:
                    table[row_index, column] = value
        self.matrix = table
        self.verdicts = numpy.array([verdict for _, _, verdict in self.rows], dtype=bool)

    def symbol_history(self) -> dict[str, tuple[int, int]]:
        found: dict[str, list[int]] = {}
        for symbol, (_, _, verdict) in zip(self.symbols, self.rows):
            if not symbol:
                continue
            counts = found.setdefault(symbol, [0, 0])
            counts[0] += 1
            counts[1] += int(verdict)
        return {symbol: (counts[0], counts[1]) for symbol, counts in found.items()}


@dataclass(frozen=True)
class CohortAppraisal:
    appraisals: list[Appraisal]
    symbol_history: dict[str, tuple[int, int]]
    base_rate: float | None

    def symbol_days(self, symbol: str) -> int:
        return self.symbol_history.get(symbol, (0, 0))[0]

    def symbol_rate(self, symbol: str, shrink: int = 10) -> float | None:
        if self.base_rate is None:
            return None
        seen, favourable = self.symbol_history.get(symbol, (0, 0))
        return round((favourable + shrink * self.base_rate) / (seen + shrink), 4)


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
        ignored_features: Sequence[str] = (),
    ) -> None:
        self.graph = graph
        self.retriever = retriever
        self.minimum_evidence = max(1, int(minimum_evidence))
        self.neighbours = max(1, int(neighbours))
        self.ignored_features = frozenset(str(name) for name in ignored_features)

    def _distance_features(self, data: dict) -> dict[str, float]:
        return {
            name: value
            for name, value in numeric_features(data).items()
            if name not in self.ignored_features
        }

    def appraise_many(
        self, record_ids: Sequence[str], *, record: bool = False, features_only: bool = False
    ) -> list[Appraisal]:
        return self.appraise_cohort(
            record_ids, record=record, features_only=features_only
        ).appraisals

    def appraise_cohort(
        self, record_ids: Sequence[str], *, record: bool = False, features_only: bool = False
    ) -> CohortAppraisal:
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
                    features_only=features_only,
                )
                for item in records
            ]
        )
        if record:
            self.record(appraisals)

        history: dict[str, tuple[int, int]] = {}
        base_rates: list[float] = []
        for pool in pools.values():
            for symbol, (seen, favourable) in pool.symbol_history().items():
                previous = history.get(symbol, (0, 0))
                history[symbol] = (previous[0] + seen, previous[1] + favourable)
            if pool.base_rate is not None:
                base_rates.append(pool.base_rate)
        base_rate = round(sum(base_rates) / len(base_rates), 4) if base_rates else None
        return CohortAppraisal(appraisals=appraisals, symbol_history=history, base_rate=base_rate)

    def record(
        self,
        appraisals: Sequence[Appraisal],
        extra: Mapping[str, Mapping[str, object]] | None = None,
    ) -> list[MemoryRecord]:
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
                    "base_rate": item.base_rate,
                    **dict((extra or {}).get(item.record_id, {})),
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

        symbols: list[str | None] = []
        for row in found:
            verdict = _json(row["outcome_json"]).get(FAVOURABLE_KEY)
            if not isinstance(verdict, bool):
                continue
            if not _precedes(row["occurred_at"], before):
                continue
            data = _json(row["data_json"])
            rows.append((str(row["id"]), self._distance_features(data), verdict))
            symbol = data.get("symbol")
            symbols.append(str(symbol) if isinstance(symbol, str) and symbol else None)

        base_rate = (
            round(sum(1 for _, _, verdict in rows if verdict) / len(rows), 4) if rows else None
        )
        return _Pool(rows=rows, scales=_scales(rows), base_rate=base_rate, symbols=symbols)

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
        features_only: bool = False,
    ) -> Appraisal:
        features = numeric_features(record.data)
        applied = self._applied(rules, features) if features else ()
        measured = self._distance_features(record.data)
        shared = [name for name in measured if name in pool.scales]
        base = self._by_features(record, pool, measured, shared) if pool and shared else None
        if base is None:
            base = (
                self._nothing_numeric(record, pool)
                if features_only
                else self._by_text(record, before)
            )
        return _with_rules(base, applied)

    def _nothing_numeric(self, record: MemoryRecord, pool: _Pool) -> Appraisal:
        return Appraisal(
            record_id=record.id,
            basis=Basis.NONE,
            sample=0,
            favourable=0,
            unfavourable=0,
            score=None,
            rationale=(
                "No closed observations share this candidate's features yet, so there is "
                "nothing to count. Text similarity was not consulted."
            ),
            topic=record.topic,
            source_ref=record.source_ref,
            base_rate=pool.base_rate,
        )

    def _by_features(
        self, record: MemoryRecord, pool: _Pool, features: dict[str, float], shared: list[str]
    ) -> Appraisal | None:
        pool.prepare()
        columns = [pool.columns[name] for name in shared]
        query = numpy.array([features[name] for name in shared], dtype=float)
        scale = numpy.array([pool.scales[name] for name in shared], dtype=float)
        diffs = numpy.abs(pool.matrix[:, columns] - query) / scale
        overlap = numpy.sum(~numpy.isnan(diffs), axis=1)
        distance = numpy.nansum(diffs, axis=1) / numpy.maximum(overlap, 1)
        usable = overlap > 0
        own = pool.positions.get(record.id)
        if own is not None:
            usable[own] = False
        candidates = numpy.flatnonzero(usable)
        if candidates.size == 0:
            return None

        count = min(self.neighbours, int(candidates.size))
        if count < candidates.size:
            chosen = candidates[numpy.argpartition(distance[candidates], count - 1)[:count]]
        else:
            chosen = candidates
        chosen = chosen[numpy.argsort(distance[chosen], kind="stable")]
        nearest = distance[chosen]
        verdicts = pool.verdicts[chosen]
        favourable = int(verdicts.sum())
        unfavourable = int(count - favourable)

        if count < self.minimum_evidence:
            return Appraisal(
                record_id=record.id,
                basis=Basis.OBSERVATIONS,
                sample=count,
                favourable=favourable,
                unfavourable=unfavourable,
                score=None,
                rationale=(
                    f"{count} of {self.minimum_evidence} resolved neighbours needed before "
                    f"a rate means anything ({favourable} favourable, {unfavourable} not)."
                ),
                topic=record.topic,
                source_ref=record.source_ref,
                base_rate=pool.base_rate,
            )

        weights = 1.0 / (1.0 + nearest)
        score = float(weights[verdicts].sum() / weights.sum())
        span = ", ".join(sorted(shared))
        return Appraisal(
            record_id=record.id,
            basis=Basis.OBSERVATIONS,
            sample=count,
            favourable=favourable,
            unfavourable=unfavourable,
            score=round(score, 4),
            rationale=(
                f"{favourable} of {count} nearest observations by {span} were favourable. "
                "The rate leans on the closest of them. An observed rate, not a prediction."
            ),
            topic=record.topic,
            source_ref=record.source_ref,
            base_rate=pool.base_rate,
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


VERDICT_APPROVE = "approve"
VERDICT_VETO = "veto"
VERDICT_ABSTAIN = "abstain"


def verdict_for(appraisal: Appraisal, *, approve_at: float, veto_at: float) -> str:
    if appraisal.score is None:
        return VERDICT_ABSTAIN
    if appraisal.score >= approve_at:
        return VERDICT_APPROVE
    if appraisal.score <= veto_at:
        return VERDICT_VETO
    return VERDICT_ABSTAIN


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
        base_rate=appraisal.base_rate,
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
            base_rate=item.base_rate,
        )
        for item in appraisals
    ]


__all__ = [
    "AppliedRule",
    "Appraisal",
    "Appraiser",
    "Basis",
    "CohortAppraisal",
    "CONTRACT",
    "FAVOURABLE_KEY",
    "METHOD_FEATURES",
    "METHOD_TEXT",
    "VERDICT_ABSTAIN",
    "VERDICT_APPROVE",
    "VERDICT_VETO",
    "numeric_features",
    "verdict_for",
]
