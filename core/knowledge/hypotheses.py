from __future__ import annotations

from dataclasses import dataclass

from core.knowledge.graph import KnowledgeGraph
from core.knowledge.links import MemoryRelation
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus


@dataclass(frozen=True)
class HypothesisPolicy:
    """When accumulated evidence is enough to call a hypothesis one way.

    Deliberately counts of evidence, never a model's self-reported confidence.
    An LLM's confidence is uncalibrated and would let a fluent guess promote
    itself; the whole point of the evidence trail is that the arithmetic runs
    on things that actually happened.
    """

    #: Below this, no verdict either way. Guards against calling a hypothesis
    #: on the first two data points that happen to agree.
    minimum_evidence: int = 5
    support_ratio: float = 0.7
    rejection_ratio: float = 0.7

    def __post_init__(self) -> None:
        if self.minimum_evidence < 1:
            raise KnowledgeError("minimum_evidence must be at least 1")
        for name in ("support_ratio", "rejection_ratio"):
            value = float(getattr(self, name))
            if not 0.5 < value <= 1.0:
                raise KnowledgeError(f"{name} must be above 0.5 and at most 1.0, got {value}")


@dataclass(frozen=True)
class Assessment:
    """What the evidence currently says, and what that implies."""

    hypothesis_id: str
    status: MemoryStatus
    recommended: MemoryStatus
    supporting: int
    contradicting: int
    rationale: str

    @property
    def total(self) -> int:
        return self.supporting + self.contradicting

    @property
    def would_change(self) -> bool:
        return self.recommended is not self.status


class HypothesisTracker:
    """Moves a hypothesis through testing on the strength of its evidence.

    The one thing it will not do is promote. A hypothesis can reach SUPPORTED
    on evidence alone, but ACCEPTED -- the status that means "reason with this"
    -- is reachable only through promote(), which cannot be called without
    naming who approved it. That is the experimental/production line the design
    asks for, enforced by the signature rather than by a comment.
    """

    def __init__(self, graph: KnowledgeGraph, policy: HypothesisPolicy | None = None) -> None:
        self.graph = graph
        self.policy = policy or HypothesisPolicy()

    # -- creating and testing ----------------------------------------------

    def propose(self, content: str, *, topic: str, source: str, **fields: object) -> MemoryRecord:
        return self.graph.records.add(
            MemoryRecord(
                kind=MemoryKind.HYPOTHESIS,
                topic=topic,
                content=content,
                source=source,
                **fields,  # type: ignore[arg-type]
            )
        )

    def begin_testing(self, hypothesis_id: str) -> MemoryRecord:
        hypothesis = self._hypothesis(hypothesis_id)
        if hypothesis.status is MemoryStatus.TESTING:
            return hypothesis
        if hypothesis.status is not MemoryStatus.PROPOSED:
            raise KnowledgeError(
                f"Only a proposed hypothesis can enter testing; this one is {hypothesis.status.value}"
            )
        self.graph.records.set_status(hypothesis_id, MemoryStatus.TESTING)
        return self._hypothesis(hypothesis_id)

    # -- reading the evidence -----------------------------------------------

    def assess(self, hypothesis_id: str) -> Assessment:
        """What the evidence says. Reads only; nothing is written."""
        hypothesis = self._hypothesis(hypothesis_id)
        counts = self.graph.links.count_relations_from(hypothesis_id)
        supporting = counts.get(MemoryRelation.SUPPORTED_BY, 0)
        contradicting = counts.get(MemoryRelation.CONTRADICTED_BY, 0)
        recommended, rationale = self._verdict(hypothesis.status, supporting, contradicting)
        return Assessment(
            hypothesis_id=hypothesis_id,
            status=hypothesis.status,
            recommended=recommended,
            supporting=supporting,
            contradicting=contradicting,
            rationale=rationale,
        )

    def _verdict(
        self, status: MemoryStatus, supporting: int, contradicting: int
    ) -> tuple[MemoryStatus, str]:
        total = supporting + contradicting

        if status in {MemoryStatus.ACCEPTED, MemoryStatus.SUPERSEDED}:
            return status, f"Already {status.value}; testing does not reopen it."
        if total == 0:
            target = MemoryStatus.PROPOSED if status is MemoryStatus.PROPOSED else status
            return target, "No evidence recorded yet."
        if total < self.policy.minimum_evidence:
            return (
                MemoryStatus.TESTING,
                f"Under test: {total} of {self.policy.minimum_evidence} pieces of evidence needed "
                f"before a verdict ({supporting} for, {contradicting} against).",
            )

        share_supporting = supporting / total
        share_contradicting = contradicting / total
        if share_supporting >= self.policy.support_ratio:
            return (
                MemoryStatus.SUPPORTED,
                f"Supported: {supporting} of {total} pieces of evidence agree "
                f"({share_supporting:.0%}, threshold {self.policy.support_ratio:.0%}). "
                "Reasoning with it still requires approval.",
            )
        if share_contradicting >= self.policy.rejection_ratio:
            return (
                MemoryStatus.REJECTED,
                f"Rejected: {contradicting} of {total} pieces of evidence disagree "
                f"({share_contradicting:.0%}, threshold {self.policy.rejection_ratio:.0%}).",
            )
        return (
            MemoryStatus.TESTING,
            f"Inconclusive: {supporting} for, {contradicting} against, neither side at threshold.",
        )

    # -- acting on it --------------------------------------------------------

    def evaluate(self, hypothesis_id: str) -> Assessment:
        """Apply what the evidence implies. Never reaches ACCEPTED.

        Moving between proposed, testing, supported and rejected is a statement
        about evidence. Moving to accepted is a statement about production
        behaviour, and only promote() can make it.
        """
        assessment = self.assess(hypothesis_id)
        if assessment.would_change and assessment.recommended is not MemoryStatus.ACCEPTED:
            self.graph.records.set_status(hypothesis_id, assessment.recommended)
        return assessment

    def promote(self, hypothesis_id: str, *, approved_by: str, note: str | None = None) -> MemoryRecord:
        """Accept a supported hypothesis, on a named person's authority.

        approved_by has no default on purpose: there is no way to call this
        without someone standing behind it.
        """
        if not str(approved_by).strip():
            raise KnowledgeError("Promotion requires naming who approved it")
        hypothesis = self._hypothesis(hypothesis_id)
        if hypothesis.status is not MemoryStatus.SUPPORTED:
            raise KnowledgeError(
                f"Only a supported hypothesis can be promoted; this one is {hypothesis.status.value}"
            )

        decision = self.graph.records.add(
            MemoryRecord(
                kind=MemoryKind.DECISION,
                topic=hypothesis.topic,
                content=note or f"Approved '{hypothesis.content}' for use.",
                source=f"approval:{approved_by}",
                data={"approved_by": approved_by, "hypothesis_id": hypothesis_id},
            )
        )
        self.graph.links.link(decision.id, hypothesis_id, MemoryRelation.DECIDED_FROM)
        self.graph.records.set_status(hypothesis_id, MemoryStatus.ACCEPTED)
        return self._hypothesis(hypothesis_id)

    # -- finding work --------------------------------------------------------

    def in_status(self, status: MemoryStatus, *, topic: str | None = None, limit: int = 50) -> list[MemoryRecord]:
        if topic is not None:
            found = self.graph.records.list_by_topic(topic, kind=MemoryKind.HYPOTHESIS, limit=limit)
            return [item for item in found if item.status is status]
        return self.graph.records.list_by_kind_and_status(MemoryKind.HYPOTHESIS, status, limit=limit)

    def awaiting_approval(self, *, topic: str | None = None, limit: int = 50) -> list[MemoryRecord]:
        """Hypotheses the evidence supports, waiting on a person."""
        return self.in_status(MemoryStatus.SUPPORTED, topic=topic, limit=limit)

    def _hypothesis(self, hypothesis_id: str) -> MemoryRecord:
        record = self.graph.records.get(hypothesis_id)
        if record is None:
            raise KnowledgeError(f"No such hypothesis: {hypothesis_id}")
        if record.kind is not MemoryKind.HYPOTHESIS:
            raise KnowledgeError(f"Not a hypothesis: {hypothesis_id} is a {record.kind.value}")
        return record
