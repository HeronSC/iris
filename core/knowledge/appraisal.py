# File: core/knowledge/appraisal.py

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from core.knowledge.graph import KnowledgeGraph
from core.knowledge.links import MemoryLink, MemoryRelation
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord
from core.knowledge.retrieval import KnowledgeQuery, KnowledgeRetriever

CONTRACT = "shadow-1"

FAVOURABLE_KEY = "favourable"

METHOD_FEATURES = "feature-knn"

METHOD_TEXT = "text-similarity"

POOL_LIMIT = 5000


class Basis(str, Enum):
    NONE = "none"
    OBSERVATIONS = "observations"


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

        pools: dict[str, _Pool] = {}
        for item in records:
            if item.topic not in pools:
                pools[item.topic] = self._pool(item.topic)

        appraisals = _ranked([self._appraise(item, pools[item.topic]) for item in records])
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

    def _pool(self, topic: str) -> _Pool:
        rows: list[tuple[str, dict[str, float], bool]] = []
        with self.graph.records.database.connect() as conn:
            found = conn.execute(
                "SELECT m.id, m.data_json, o.data_json AS outcome_json "
                "FROM memories m "
                "JOIN memory_links l ON l.target_id = m.id AND l.relation = ? "
                "JOIN memories o ON o.id = l.source_id "
                "WHERE m.topic = ? AND m.kind = ? "
                "ORDER BY m.sequence DESC LIMIT ?",
                (MemoryRelation.OUTCOME_OF.value, topic, MemoryKind.OBSERVATION.value, POOL_LIMIT),
            ).fetchall()

        for row in found:
            verdict = _json(row["outcome_json"]).get(FAVOURABLE_KEY)
            if not isinstance(verdict, bool):
                continue
            rows.append((str(row["id"]), numeric_features(_json(row["data_json"])), verdict))

        return _Pool(rows=rows, scales=_scales(rows))

    def _appraise(self, record: MemoryRecord, pool: _Pool) -> Appraisal:
        features = numeric_features(record.data)
        shared = [name for name in features if name in pool.scales]
        if pool and shared:
            return self._by_features(record, pool, features, shared)
        return self._by_text(record)

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

    def _by_text(self, record: MemoryRecord) -> Appraisal:
        similar = self._similar(record)
        if not similar:
            return Appraisal(
                record_id=record.id,
                basis=Basis.NONE,
                sample=0,
                favourable=0,
                unfavourable=0,
                score=None,
                rationale="Nothing resembling this has been seen before.",
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

    def _similar(self, record: MemoryRecord) -> list[MemoryRecord]:
        result = self.retriever.retrieve(
            KnowledgeQuery(
                text=record.content,
                topic=record.topic,
                kinds=(MemoryKind.OBSERVATION,),
                limit=self.neighbours,
                candidate_limit=max(self.neighbours * 4, 200),
            )
        )
        return [item.record for item in result.records if item.record.id != record.id]


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
        )
        for item in appraisals
    ]


__all__ = [
    "Appraisal",
    "Appraiser",
    "Basis",
    "CONTRACT",
    "FAVOURABLE_KEY",
    "METHOD_FEATURES",
    "METHOD_TEXT",
    "numeric_features",
]
