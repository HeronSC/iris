# File: core/scheduler/defaults.py

from __future__ import annotations

from core.scheduler.jobs import AUDIT_RETENTION, DATABASE_BACKUP, HYPOTHESIS_REVIEW
from core.scheduler.models import JobDefinition
from core.scheduler.service import ScheduleService

DEFAULT_JOBS: tuple[JobDefinition, ...] = (
    JobDefinition(job=HYPOTHESIS_REVIEW, name="Re-appraise hypotheses", cron="0 6 * * *", id="hypothesis-review"),
    JobDefinition(job=DATABASE_BACKUP, name="Back up Data", cron="0 3 * * *", id="database-backup", channels=("inbox", "log")),
    JobDefinition(job=AUDIT_RETENTION, name="Trim audit and traces", cron="30 3 * * *", id="audit-retention", channels=("log",), params={"keep_days": 90}),
)


def ensure_default_jobs(schedules: ScheduleService) -> list[JobDefinition]:
    return [schedules.ensure(definition) for definition in DEFAULT_JOBS if definition.job in schedules.jobs]


__all__ = ["DEFAULT_JOBS", "ensure_default_jobs"]
