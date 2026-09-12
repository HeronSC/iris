# File: core/scheduler/jobs.py

from __future__ import annotations

import logging
from typing import Any, Callable

from core.audit.retention import apply_retention
from core.knowledge.models import MemoryStatus
from core.scheduler.models import JobResult

logger = logging.getLogger(__name__)

HYPOTHESIS_REVIEW = "hypothesis_review"

DATABASE_BACKUP = "database_backup"

WORKFLOW_RUN = "workflow_run"

AUDIT_RETENTION = "audit_retention"


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


def database_backup(backups: Any) -> Callable[[dict[str, Any]], JobResult]:
    def run(_params: dict[str, Any]) -> JobResult:
        report = backups.run()
        return JobResult(
            ok=report.ok,
            summary=report.summary,
            data={
                "run": report.run.name,
                "databases": sorted(report.databases),
                "folders": sorted(report.folders),
                "failures": dict(report.failures),
                "pruned": list(report.pruned),
            },
            notify=not report.ok,
        )

    return run


def audit_retention(files: Callable[[], list[Any]], *, capture_folders: Callable[[], list[Any]] | None = None, keep_days: int = 90) -> Callable[[dict[str, Any]], JobResult]:
    def run(params: dict[str, Any]) -> JobResult:
        days = int(params.get("keep_days") or keep_days)
        report = apply_retention(files(), keep_days=days, capture_folders=capture_folders() if capture_folders else ())
        return JobResult(ok=True, summary=report.summary(), data={"kept_days": report.kept_days, "removed": report.removed, "files": {name: list(counts) for name, counts in report.files.items()}}, notify=False)

    return run


def workflow_run(workflows: Any) -> Callable[[dict[str, Any]], JobResult]:
    def run(params: dict[str, Any]) -> JobResult:
        workflow_id = str(params.get("workflow_id") or "").strip()
        if not workflow_id:
            return JobResult(ok=False, summary="workflow_run needs workflow_id")
        run_record = workflows.run(workflow_id, payload={"scheduled": True}, trigger="schedule")
        if run_record is None:
            return JobResult(ok=False, summary=f"No enabled workflow matches {workflow_id}")
        return JobResult(
            ok=run_record.status in {"success", "awaiting_approval"},
            summary=run_record.summary,
            data={"run_id": run_record.id, "status": run_record.status},
            notify=run_record.status in {"failed", "partial", "blocked"},
        )

    return run


def build_jobs(
    review: Any | None = None, backups: Any | None = None, workflows: Any | None = None, retention: Callable[[dict[str, Any]], JobResult] | None = None
) -> dict[str, Callable[[dict[str, Any]], JobResult]]:
    jobs: dict[str, Callable[[dict[str, Any]], JobResult]] = {}
    if retention is not None:
        jobs[AUDIT_RETENTION] = retention
    if review is not None:
        jobs[HYPOTHESIS_REVIEW] = hypothesis_review(review)
    if backups is not None:
        jobs[DATABASE_BACKUP] = database_backup(backups)
    if workflows is not None:
        jobs[WORKFLOW_RUN] = workflow_run(workflows)
    return jobs


__all__ = ["AUDIT_RETENTION", "DATABASE_BACKUP", "HYPOTHESIS_REVIEW", "WORKFLOW_RUN", "audit_retention", "build_jobs", "database_backup", "hypothesis_review", "workflow_run"]
