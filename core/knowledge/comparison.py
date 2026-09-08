# File: core/knowledge/comparison.py

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt

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
    distinct_scores: int

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
        return _leader(self.rankers, self.base_rate)


def _by_hit_rate(rankers: list[RankerScore]) -> list[RankerScore]:
    return sorted(rankers, key=lambda item: (-item.hit_rate, -item.top_n, item.source))


def _leader(rankers: list[RankerScore], base_rate: float | None) -> RankerScore | None:
    ordered = _by_hit_rate(rankers)
    if not ordered:
        return None
    best = ordered[0]
    if best.distinct_scores < 2:
        return None
    if not _beats_base(best, base_rate):
        return None
    if len(ordered) > 1 and not _separated(best, ordered[1]):
        return None
    return best


def _beats_base(ranker: RankerScore, base_rate: float | None) -> bool:
    if base_rate is None:
        return False
    gap = ranker.hit_rate - base_rate
    if gap <= 0:
        return False
    return gap > sqrt(ranker.hit_rate * (1.0 - ranker.hit_rate) / ranker.top_n)


def _separated(best: RankerScore, next_best: RankerScore) -> bool:
    gap = best.hit_rate - next_best.hit_rate
    if gap <= 0:
        return False
    noise = sqrt(
        best.hit_rate * (1.0 - best.hit_rate) / best.top_n
        + next_best.hit_rate * (1.0 - next_best.hit_rate) / next_best.top_n
    )
    return gap > noise


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
                distinct_scores=len(set(scores.values())),
            )
        )

    for ranker in rankers:
        if ranker.distinct_scores < 2:
            notes.append(
                f"{ranker.source} gave all {ranker.scored} candidates the same score. It "
                f"expressed no order, so its top {ranker.top_n} is an arbitrary slice."
            )
            continue
        if ranker.distinct_scores * 4 < ranker.scored:
            notes.append(
                f"{ranker.source} separated {ranker.scored} candidates into only "
                f"{ranker.distinct_scores} distinct scores, so much of its order is arbitrary."
            )
        if ranker.tied_at_the_cut:
            notes.append(
                f"{ranker.source} has {ranker.tied_at_the_cut} candidates tied at the cut, so "
                "which ones made the top is partly arbitrary."
            )

    standing = _by_hit_rate(rankers)
    if standing and standing[0].distinct_scores >= 2:
        best = standing[0]
        if not _beats_base(best, base_rate):
            notes.append(
                f"{best.source} tops the field but does not clear the {base_rate:.1%} base rate "
                f"by more than the noise at the top {best.top_n}. This cohort does not name a "
                "leader."
            )
        elif len(standing) > 1 and not _separated(best, standing[1]):
            next_best = standing[1]
            gap = best.hit_rate - next_best.hit_rate
            standing_note = (
                f"is level with {next_best.source}"
                if gap <= 0
                else f"leads {next_best.source} by {gap * 100:.1f} points"
            )
            notes.append(
                f"{best.source} {standing_note} at the top {best.top_n}, which is inside the "
                f"noise on {len(resolved)} candidates. This cohort does not name a leader."
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
        for ranker in _by_hit_rate(comparison.rankers):
            lines.append(f"  {ranker.summary()}")
        leader = comparison.leader
        if leader is not None:
            lines.append(f"Leader: {leader.source}.")
        elif comparison.rankers:
            lines.append("No leader on this cohort.")
    for note in comparison.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


__all__ = ["Comparison", "RankerScore", "compare_rankers", "render_comparison"]
