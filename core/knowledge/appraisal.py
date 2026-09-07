# File: core/knowledge/appraisal.py

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from core.knowledge.graph import KnowledgeGraph
from core.knowledge.links import MemoryLink, MemoryRelation
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord
from core.knowledge.retrieval import KnowledgeQuery, KnowledgeRetriever

CONTRACT = "shadow-1"

FAVOURABLE_KEY = "favourable"


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

    @property
    def binding(self) -> bool:
        return False


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
        appraisals = _ranked([self._appraise(record_id) for record_id in record_ids])
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

    def _appraise(self, record_id: str) -> Appraisal:
        record = self.graph.records.get(record_id)
        if record is None:
            raise KnowledgeError(f"No such memory: {record_id}")

        similar = self._similar(record)
        if not similar:
            return Appraisal(
                record_id=record_id,
                basis=Basis.NONE,
                sample=0,
                favourable=0,
                unfavourable=0,
                score=None,
                rationale="Nothing resembling this has been seen before.",
                topic=record.topic,
                source_ref=record.source_ref,
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
                record_id=record_id,
                basis=Basis.NONE,
                sample=0,
                favourable=0,
                unfavourable=0,
                score=None,
                rationale=_nothing_to_go_on(len(similar), len(outcomes), unlabelled),
                topic=record.topic,
                source_ref=record.source_ref,
            )

        if sample < self.minimum_evidence:
            return Appraisal(
                record_id=record_id,
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
            )

        return Appraisal(
            record_id=record_id,
            basis=Basis.OBSERVATIONS,
            sample=sample,
            favourable=favourable,
            unfavourable=unfavourable,
            score=round(favourable / sample, 4),
            rationale=(
                f"{favourable} of {sample} resolved outcomes on comparable observations were "
                f"favourable. An observed rate, not a prediction."
            ),
            topic=record.topic,
            source_ref=record.source_ref,
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
        )
        for item in appraisals
    ]


__all__ = ["Appraisal", "Appraiser", "Basis", "CONTRACT", "FAVOURABLE_KEY"]
