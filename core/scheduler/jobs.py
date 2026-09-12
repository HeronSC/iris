# File: core/scheduler/jobs.py

from __future__ import annotations

import logging
from typing import Any, Callable

from core.knowledge.models import MemoryStatus
from core.scheduler.models import JobResult

logger = logging.getLogger(__name__)

HYPOTHESIS_REVIEW = "hypothesis_review"


def hypothesis_review(review: Any) -> Callable[[dict[str, Any]], JobResult]:
    def run(params: dict[str, Any]) -> JobResult:
        topic = str(params.get("topic") or "").strip().lower() or None
        limit = int(params.get("limit", 200))
        before = {item.hypothesis.id for item in review.pending(topic=topic, limit=limit)}
        moved = review.refresh(topic=topic, limit=limit)
        waiting = review.pending(topic=topic, limit=limit)
        newly_waiting = [item for item in waiting if item.hypothesis.id not in before]
        under_test = len(review.under_test(topic=topic, limit=limit))

        where = f" under {topic}" if topic else ""
        if newly_waiting:
            lines = [f"{len(newly_waiting)} hypothesis(es){where} now have the evidence to be accepted:"]
            for item in newly_waiting[:5]:
                lines.append(f"- {item.hypothesis.content} ({item.assessment.supporting} for, {item.assessment.contradicting} against)")
            lines.append("/knowledge review to approve or decline.")
            summary = "\n".join(lines)
        elif moved:
            summary = f"{moved} hypothesis(es){where} changed status; {len(waiting)} waiting on approval, {under_test} still under test"
        else:
            summary = f"Nothing moved{where}; {len(waiting)} waiting on approval, {under_test} still under test"

        return JobResult(
            ok=True,
            summary=summary,
            data={
                "moved": moved,
                "waiting": len(waiting),
                "under_test": under_test,
                "newly_waiting": [item.hypothesis.id for item in newly_waiting],
                "status": MemoryStatus.SUPPORTED.value,
            },
            notify=bool(newly_waiting),
        )

    return run


def build_jobs(review: Any | None = None) -> dict[str, Callable[[dict[str, Any]], JobResult]]:
    jobs: dict[str, Callable[[dict[str, Any]], JobResult]] = {}
    if review is not None:
        jobs[HYPOTHESIS_REVIEW] = hypothesis_review(review)
    return jobs


__all__ = ["HYPOTHESIS_REVIEW", "build_jobs", "hypothesis_review"]
