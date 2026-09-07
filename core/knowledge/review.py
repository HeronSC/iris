from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.knowledge.graph import KnowledgeGraph, render_explanation
from core.knowledge.hypotheses import Assessment, HypothesisTracker
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus


@dataclass(frozen=True)
class PendingPromotion:
    """A hypothesis the evidence supports, waiting on a person."""

    hypothesis: MemoryRecord
    assessment: Assessment

    def summary(self) -> str:
        return (
            f"{self.hypothesis.id[:8]}  {self.hypothesis.topic}\n"
            f"  {self.hypothesis.content}\n"
            f"  {self.assessment.rationale}"
        )


class KnowledgeReviewWorkflow:
    """The gate between what the evidence supports and what Iris acts on.

    Nothing here decides anything. It lists what is waiting, applies the
    decision a person made, and writes both outcomes to the same audit log the
    profile approval path uses, so there is one place to answer "what was
    approved, by whom, and when".
    """

    def __init__(
        self,
        tracker: HypothesisTracker,
        audit_logger: Any | None = None,
    ) -> None:
        self.tracker = tracker
        self.audit_logger = audit_logger

    @property
    def graph(self) -> KnowledgeGraph:
        return self.tracker.graph

    def refresh(self, *, topic: str | None = None, limit: int = 50) -> int:
        """Re-assess everything under test, so the queue reflects current evidence.

        Evidence arrives without anyone asking a hypothesis whether it has
        changed its mind, so the queue is only honest if something re-runs the
        verdicts. Returns how many statuses moved.
        """
        moved = 0
        for status in (MemoryStatus.PROPOSED, MemoryStatus.TESTING):
            for hypothesis in self.tracker.in_status(status, topic=topic, limit=limit):
                if self.tracker.evaluate(hypothesis.id).would_change:
                    moved += 1
        return moved

    def pending(self, *, topic: str | None = None, limit: int = 50) -> list[PendingPromotion]:
        return [
            PendingPromotion(hypothesis=item, assessment=self.tracker.assess(item.id))
            for item in self.tracker.awaiting_approval(topic=topic, limit=limit)
        ]

    def approve(self, hypothesis_id: str, *, approved_by: str, note: str | None = None) -> MemoryRecord:
        record = self.tracker.promote(hypothesis_id, approved_by=approved_by, note=note)
        self._audit("approved", record, actor=approved_by, detail=note)
        return record

    def decline(self, hypothesis_id: str, *, declined_by: str, reason: str) -> MemoryRecord:
        record = self.tracker.decline(hypothesis_id, declined_by=declined_by, reason=reason)
        self._audit("declined", record, actor=declined_by, detail=reason)
        return record

    def explain(self, hypothesis_id: str, *, max_depth: int = 3) -> str:
        record = self.graph.records.get(hypothesis_id)
        if record is None:
            raise KnowledgeError(f"No such memory: {hypothesis_id}")
        return render_explanation(self.graph.explain(hypothesis_id, max_depth=max_depth))

    def matches(self, needle: str) -> list[MemoryRecord]:
        """Every record a full or shortened id could mean.

        Any kind, not only hypotheses: an observation needs to be nameable too,
        both to close it with an outcome and to ask why it is believed.
        """
        exact = self.graph.records.get(needle)
        if exact is not None:
            return [exact]
        prefix = str(needle).strip().lower()
        if len(prefix) < 4:
            return []
        return self.graph.records.find_by_id_prefix(prefix)

    def find(self, needle: str) -> MemoryRecord | None:
        """The one record this names, or None if it names none or several."""
        found = self.matches(needle)
        return found[0] if len(found) == 1 else None

    def observe(self, topic: str, content: str, *, source: str, **fields: Any) -> MemoryRecord:
        """Record something seen. The plainest way into the store."""
        return self.graph.records.add(
            MemoryRecord(
                kind=MemoryKind.OBSERVATION,
                topic=topic,
                content=content,
                source=source,
                **fields,
            )
        )

    def close(self, observation_id: str, content: str, *, source: str) -> MemoryRecord:
        """Record what came of an observation, and link the two."""
        return self.graph.record_outcome(
            observation_id,
            MemoryRecord(
                kind=MemoryKind.OUTCOME,
                topic=self._observation(observation_id).topic,
                content=content,
                source=source,
            ),
        )

    def open_observations(self, *, topic: str | None = None, limit: int = 20) -> list[MemoryRecord]:
        if topic is not None:
            return self.graph.open_observations(topic, limit=limit)
        topics = {
            record.topic
            for record in self.graph.records.list_by_kind_and_status(
                MemoryKind.OBSERVATION, MemoryStatus.OBSERVED, limit=200
            )
        }
        found: list[MemoryRecord] = []
        for name in sorted(topics):
            found.extend(self.graph.open_observations(name, limit=limit))
        return found[:limit]

    def _observation(self, observation_id: str) -> MemoryRecord:
        record = self.graph.records.get(observation_id)
        if record is None:
            raise KnowledgeError(f"No such observation: {observation_id}")
        if record.kind is not MemoryKind.OBSERVATION:
            raise KnowledgeError(
                f"Outcomes close observations, and that is a {record.kind.value}"
            )
        return record

    def _audit(self, action: str, hypothesis: MemoryRecord, *, actor: str, detail: str | None) -> None:
        if self.audit_logger is None:
            return
        assessment = self.tracker.assess(hypothesis.id)
        self.audit_logger.log(
            {
                "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "action": action,
                "subject": "hypothesis",
                "hypothesis_id": hypothesis.id,
                "topic": hypothesis.topic,
                "content": hypothesis.content,
                "actor": actor,
                "detail": detail,
                "status": hypothesis.status.value,
                "supporting": assessment.supporting,
                "contradicting": assessment.contradicting,
            }
        )


__all__ = ["KnowledgeReviewWorkflow", "PendingPromotion", "MemoryKind"]
