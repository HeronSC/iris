# File: core/knowledge/comparison.py

from __future__ import annotations

from dataclasses import dataclass, field

from core.knowledge.appraisal import FAVOURABLE_KEY
from core.knowledge.graph import KnowledgeGraph
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord


@dataclass(frozen=True)
class RankerScore:
    source: str
    scored: int
    top_n: int
    hits: int
    hit_rate: float
    lift: float
    tied_at_the_cut: int

    def summary(self) -> str:
        direction = "above" if self.lift > 0 else "below" if self.lift < 0 else "level with"
        return (
            f"{self.source}: {self.hits} of {self.top_n} favourable "
            f"({self.hit_rate:.1%}), {abs(self.lift):.1%} {direction} the base rate"
        )


@dataclass(frozen=True)
class Comparison:
    cohort: str
    size: int
    resolved: int
    base_rate: float | None
    top_n: int
    rankers: list[RankerScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def leader(self) -> RankerScore | None:
        if not self.rankers:
            return None
        return max(self.rankers, key=lambda item: (item.hit_rate, item.top_n))


def compare_rankers(
    graph: KnowledgeGraph, cohort: str, *, top_n: int = 50, minimum_resolved: int = 20
) -> Comparison:
    key = str(cohort or "").strip()
    if not key:
        raise KnowledgeError("A comparison needs a cohort to compare")

    observations = graph.records.list_by_source_ref(key, kind=MemoryKind.OBSERVATION)
    if not observations:
        raise KnowledgeError(f"No observations were recorded under source_ref {key}")

    ids = [item.id for item in observations]
    outcomes = graph.outcomes_for(ids)
    resolved: dict[str, bool] = {}
    for observation_id, outcome in outcomes.items():
        verdict = outcome.data.get(FAVOURABLE_KEY)
        if isinstance(verdict, bool):
            resolved[observation_id] = verdict

    notes: list[str] = []
    if len(resolved) < len(observations):
        notes.append(
            f"{len(observations) - len(resolved)} of {len(observations)} candidates carry no "
            "favourable outcome yet and take no part in this."
        )
    if not resolved:
        return Comparison(
            cohort=key, size=len(observations), resolved=0, base_rate=None, top_n=top_n, notes=notes
        )

    base_rate = sum(1 for good in resolved.values() if good) / len(resolved)
    if len(resolved) < minimum_resolved:
        notes.append(
            f"Only {len(resolved)} resolved outcomes; below {minimum_resolved} the difference "
            "between two rankers is noise."
        )

    cut = max(1, min(int(top_n), len(resolved)))
    if cut < top_n:
        notes.append(f"Asked for the top {top_n}, but only {cut} candidates are resolved.")

    decisions = graph.decisions_for(list(resolved))
    by_source: dict[str, dict[str, float]] = {}
    for observation_id, records in decisions.items():
        if observation_id not in resolved:
            continue
        for record in records:
            if record.confidence is None:
                continue
            by_source.setdefault(record.source, {})[observation_id] = float(record.confidence)

    rankers: list[RankerScore] = []
    for source in sorted(by_source):
        scores = by_source[source]
        if len(scores) < cut:
            notes.append(
                f"{source} scored only {len(scores)} of the {len(resolved)} resolved candidates."
            )
        ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        chosen = ordered[:cut]
        if not chosen:
            continue
        boundary = chosen[-1][1]
        tied = sum(1 for _, value in ordered if value == boundary)
        hits = sum(1 for observation_id, _ in chosen if resolved[observation_id])
        hit_rate = hits / len(chosen)
        rankers.append(
            RankerScore(
                source=source,
                scored=len(scores),
                top_n=len(chosen),
                hits=hits,
                hit_rate=round(hit_rate, 4),
                lift=round(hit_rate - base_rate, 4),
                tied_at_the_cut=tied if tied > 1 else 0,
            )
        )

    for ranker in rankers:
        if ranker.tied_at_the_cut:
            notes.append(
                f"{ranker.source} has {ranker.tied_at_the_cut} candidates tied at the cut, so "
                "which ones made the top is partly arbitrary."
            )
    if not rankers:
        notes.append("No ranker scored any resolved candidate, so there is nothing to compare.")

    return Comparison(
        cohort=key,
        size=len(observations),
        resolved=len(resolved),
        base_rate=round(base_rate, 4),
        top_n=cut,
        rankers=rankers,
        notes=notes,
    )


def render_comparison(comparison: Comparison) -> str:
    lines = [
        f"Cohort {comparison.cohort}: {comparison.size} candidates, "
        f"{comparison.resolved} with a known outcome."
    ]
    if comparison.base_rate is None:
        lines.append("Nothing is resolved yet, so there is no base rate and nothing to compare.")
    else:
        lines.append(f"Base rate {comparison.base_rate:.1%}. Hit rate at top {comparison.top_n}:")
        for ranker in sorted(comparison.rankers, key=lambda item: -item.hit_rate):
            lines.append(f"  {ranker.summary()}")
    for note in comparison.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


__all__ = ["Comparison", "RankerScore", "compare_rankers", "render_comparison"]
