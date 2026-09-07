from __future__ import annotations

from dataclasses import dataclass, field

from core.knowledge.links import (
    EVIDENCE_RELATIONS,
    GROUNDING_RELATIONS,
    LinkRepository,
    MemoryLink,
    MemoryRelation,
)
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.repository import MEMORY_COLUMNS, KnowledgeRepository, record_from_row
from core.storage.sqlite_database import SQLiteDatabase


@dataclass(frozen=True)
class Evidence:
    """What is known for and against one claim."""

    supporting: list[tuple[MemoryRecord, MemoryLink]] = field(default_factory=list)
    contradicting: list[tuple[MemoryRecord, MemoryLink]] = field(default_factory=list)

    @property
    def balance(self) -> int:
        """Supporting minus contradicting.

        A count, not a probability. Deliberately crude: promoting a hypothesis
        should rest on measured outcomes, not on an arithmetic that looks more
        precise than the evidence behind it.
        """
        return len(self.supporting) - len(self.contradicting)


@dataclass(frozen=True)
class Explanation:
    """One step of "why does Iris believe this", with its grounds beneath it."""

    record: MemoryRecord
    relation: MemoryRelation | None = None
    grounds: list["Explanation"] = field(default_factory=list)
    truncated: bool = False

    def flatten(self) -> list[MemoryRecord]:
        out = [self.record]
        for ground in self.grounds:
            out.extend(ground.flatten())
        return out


class KnowledgeGraph:
    """Records plus the edges between them.

    This is what makes the store answer the questions the phase is actually
    for: what happened after we saw something, what argues for and against an
    idea, and how a belief traces back to what was observed.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.records = KnowledgeRepository(database)
        self.links = LinkRepository(database)

    # -- observation and outcome ------------------------------------------

    def record_outcome(self, observation_id: str, outcome: MemoryRecord) -> MemoryRecord:
        """Store an outcome and attach it to the observation it closes."""
        observation = self.records.get(observation_id)
        if observation is None:
            raise KnowledgeError(f"No such observation: {observation_id}")
        if outcome.kind is not MemoryKind.OUTCOME:
            raise KnowledgeError(f"Expected an outcome record, got {outcome.kind.value}")
        stored = self.records.add(outcome)
        self.links.link(stored.id, observation_id, MemoryRelation.OUTCOME_OF)
        return stored

    def outcome_for(self, observation_id: str) -> MemoryRecord | None:
        """What happened after this observation, if anything has closed it yet."""
        incoming = self.links.links_to(observation_id, MemoryRelation.OUTCOME_OF)
        if not incoming:
            return None
        return self.records.get(incoming[0].source_id)

    def open_observations(self, topic: str, limit: int = 50) -> list[MemoryRecord]:
        """Observations still waiting on an outcome.

        One anti-join rather than a lookup per row: asking about a day's worth
        of candidates is the expected use, and the per-row version cost 163ms
        at 500 rows because it scaled with the limit.
        """
        columns = ", ".join(f"m.{name.strip()}" for name in MEMORY_COLUMNS.split(","))
        with self.records.database.connect() as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM memories m "
                "WHERE m.topic = ? AND m.kind = ? AND m.status != ? "
                "AND NOT EXISTS (SELECT 1 FROM memory_links l "
                "                WHERE l.target_id = m.id AND l.relation = ?) "
                "ORDER BY m.created_at DESC, m.sequence DESC LIMIT ?",
                (
                    topic,
                    MemoryKind.OBSERVATION.value,
                    MemoryStatus.SUPERSEDED.value,
                    MemoryRelation.OUTCOME_OF.value,
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [record_from_row(row) for row in rows]

    # -- hypotheses and evidence -------------------------------------------

    def add_evidence(
        self,
        hypothesis_id: str,
        evidence: MemoryRecord,
        *,
        supports: bool,
        weight: float | None = None,
        note: str | None = None,
    ) -> MemoryRecord:
        """Attach a record as evidence for or against a hypothesis.

        The evidence record is stored if it is new, so callers can hand over a
        fresh measurement without a separate add.
        """
        hypothesis = self.records.get(hypothesis_id)
        if hypothesis is None:
            raise KnowledgeError(f"No such hypothesis: {hypothesis_id}")
        if hypothesis.kind is not MemoryKind.HYPOTHESIS:
            raise KnowledgeError(f"Evidence attaches to a hypothesis, not a {hypothesis.kind.value}")

        stored = self.records.get(evidence.id) or self.records.add(evidence)
        relation = MemoryRelation.SUPPORTED_BY if supports else MemoryRelation.CONTRADICTED_BY
        self.links.link(hypothesis_id, stored.id, relation, weight=weight, note=note)
        return stored

    def evidence_for(self, hypothesis_id: str) -> Evidence:
        supporting: list[tuple[MemoryRecord, MemoryLink]] = []
        contradicting: list[tuple[MemoryRecord, MemoryLink]] = []
        for link in self.links.links_from(hypothesis_id):
            if link.relation not in EVIDENCE_RELATIONS:
                continue
            record = self.records.get(link.target_id)
            if record is None:
                continue
            bucket = supporting if link.relation is MemoryRelation.SUPPORTED_BY else contradicting
            bucket.append((record, link))
        return Evidence(supporting=supporting, contradicting=contradicting)

    # -- provenance ---------------------------------------------------------

    def explain(self, memory_id: str, *, max_depth: int = 4) -> Explanation:
        """Why Iris believes this, traced back through what grounds it.

        Cycles and re-visits are pruned rather than followed, and the walk
        stops at max_depth, so a densely linked store cannot make this run
        away. A node cut short is marked truncated instead of silently
        looking like a leaf.
        """
        record = self.records.get(memory_id)
        if record is None:
            raise KnowledgeError(f"No such memory: {memory_id}")
        return self._explain(record, relation=None, depth=max_depth, seen={memory_id})

    def _explain(
        self,
        record: MemoryRecord,
        *,
        relation: MemoryRelation | None,
        depth: int,
        seen: set[str],
    ) -> Explanation:
        outgoing = [
            link for link in self.links.links_from(record.id) if link.relation in GROUNDING_RELATIONS
        ]
        if depth <= 0:
            return Explanation(record=record, relation=relation, truncated=bool(outgoing))

        grounds: list[Explanation] = []
        truncated = False
        for link in outgoing:
            if link.target_id in seen:
                truncated = True
                continue
            target = self.records.get(link.target_id)
            if target is None:
                continue
            seen.add(link.target_id)
            grounds.append(self._explain(target, relation=link.relation, depth=depth - 1, seen=seen))
        return Explanation(record=record, relation=relation, grounds=grounds, truncated=truncated)


def render_explanation(explanation: Explanation, *, indent: int = 0) -> str:
    """A readable answer to "why does Iris believe this", for a person or a prompt."""
    prefix = "  " * indent
    lead = f"{prefix}- " if indent else ""
    because = f" ({explanation.relation.value.replace('_', ' ')})" if explanation.relation else ""
    line = f"{lead}{explanation.record.content}{because}"
    if explanation.truncated:
        line += " [...]"
    lines = [line]
    for ground in explanation.grounds:
        lines.append(render_explanation(ground, indent=indent + 1))
    return "\n".join(lines)
