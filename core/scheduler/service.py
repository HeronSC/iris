# File: core/scheduler/service.py

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.audit.stream import AuditCategory, AuditEvent, AuditStream
from core.scheduler.models import JobDefinition, JobResult, JobState, utc_now_iso
from core.watchers.models import Notification
from core.watchers.notify import Notifier, QuietHours

logger = logging.getLogger(__name__)

JobHandler = Callable[[dict[str, Any]], JobResult]


class ScheduleService:
    def __init__(
        self,
        definitions_path: str | Path,
        state_path: str | Path,
        jobs: dict[str, JobHandler],
        *,
        notifiers: dict[str, Notifier] | None = None,
        quiet_hours: QuietHours | None = None,
        audit: AuditStream | None = None,
        clock: Any = None,
    ) -> None:
        self.definitions_path = Path(definitions_path)
        self.state_path = Path(state_path)
        self.jobs = dict(jobs)
        self.notifiers = dict(notifiers or {})
        self.quiet_hours = quiet_hours or QuietHours()
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._definitions: dict[str, JobDefinition] = {}
        self._states: dict[str, JobState] = {}
        self._lock = threading.RLock()
        self._scheduler: Any = None
        self.load()

    def load(self) -> None:
        with self._lock:
            self._definitions = {}
            self._states = {}
            for payload in self._read_list(self.definitions_path):
                try:
                    definition = JobDefinition.from_json(payload)
                except (KeyError, TypeError, ValueError) as error:
                    logger.warning("Skipping a scheduled job: %s", error)
                    continue
                if definition.job not in self.jobs:
                    logger.warning("Scheduled job %s is not registered; leaving it alone", definition.job)
                self._definitions[definition.id] = definition
            stored = self._read_dict(self.state_path).get("jobs", {})
            for job_id, payload in stored.items() if isinstance(stored, dict) else []:
                if job_id in self._definitions and isinstance(payload, dict):
                    self._states[job_id] = JobState.from_json(payload)
            for job_id in self._definitions:
                self._states.setdefault(job_id, JobState())

    def save(self) -> None:
        with self._lock:
            self.definitions_path.parent.mkdir(parents=True, exist_ok=True)
            self.definitions_path.write_text(
                json.dumps([item.to_json() for item in self._definitions.values()], indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self._save_state()

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": {job_id: state.to_json() for job_id, state in self._states.items()}}
        self.state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    @staticmethod
    def _read_list(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning("Could not read %s: %s", path, error)
            return []
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    @staticmethod
    def _read_dict(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def definitions(self) -> list[JobDefinition]:
        with self._lock:
            return sorted(self._definitions.values(), key=lambda item: item.created_at)

    def get(self, job_id: str) -> JobDefinition | None:
        with self._lock:
            if job_id in self._definitions:
                return self._definitions[job_id]
            matches = [item for key, item in self._definitions.items() if key.startswith(job_id) or item.job == job_id]
            return matches[0] if len(matches) == 1 else None

    def state(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._states.get(job_id)

    def add(self, definition: JobDefinition) -> JobDefinition:
        if definition.job not in self.jobs:
            raise ValueError(f"Unknown job: {definition.job}. Known: {', '.join(sorted(self.jobs))}")
        with self._lock:
            self._definitions[definition.id] = definition
            self._states[definition.id] = JobState()
            self.save()
        self._schedule(definition)
        return definition

    def remove(self, job_id: str) -> JobDefinition | None:
        definition = self.get(job_id)
        if definition is None:
            return None
        with self._lock:
            self._definitions.pop(definition.id, None)
            self._states.pop(definition.id, None)
            self.save()
        self._unschedule(definition.id)
        return definition

    def set_enabled(self, job_id: str, enabled: bool) -> JobDefinition | None:
        definition = self.get(job_id)
        if definition is None:
            return None
        updated = JobDefinition(**{**definition.__dict__, "enabled": enabled})
        with self._lock:
            self._definitions[updated.id] = updated
            self.save()
        if enabled:
            self._schedule(updated)
        else:
            self._unschedule(updated.id)
        return updated

    def ensure(self, definition: JobDefinition) -> JobDefinition:
        existing = [item for item in self.definitions() if item.job == definition.job]
        if existing:
            return existing[0]
        return self.add(definition)

    def start(self) -> None:
        if self._scheduler is not None:
            return
        #! @allow-local-import
        from apscheduler.schedulers.background import BackgroundScheduler

        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.start()
        for definition in self.definitions():
            if definition.enabled:
                self._schedule(definition)
        logger.info("Scheduled jobs started: %d defined", len(self._definitions))

    def stop(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception as error:
                logger.warning("Scheduler shutdown: %s", error)

    @property
    def running(self) -> bool:
        return self._scheduler is not None

    def _trigger(self, definition: JobDefinition) -> Any:
        #! @allow-local-import
        from apscheduler.triggers.cron import CronTrigger
        #! @allow-local-import
        from apscheduler.triggers.interval import IntervalTrigger

        if definition.cron:
            minute, hour, day, month, day_of_week = str(definition.cron).split()
            return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week)
        return IntervalTrigger(seconds=max(30, int(definition.interval_seconds or 0)))

    def _schedule(self, definition: JobDefinition) -> None:
        if self._scheduler is None or not definition.enabled:
            return
        self._scheduler.add_job(
            self.run,
            self._trigger(definition),
            args=[definition.id],
            id=f"job-{definition.id}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )

    def _unschedule(self, job_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(f"job-{job_id}")
        except Exception:
            pass

    def run(self, job_id: str) -> JobResult | None:
        definition = self.get(job_id)
        if definition is None:
            return None
        handler = self.jobs.get(definition.job)
        if handler is None:
            return self._finish(definition, JobResult(ok=False, summary=f"No handler for {definition.job}"))
        try:
            result = handler(dict(definition.params))
        except Exception as error:
            logger.warning("Scheduled job %s failed: %s", definition.label, error)
            result = JobResult(ok=False, summary=str(error))
        return self._finish(definition, result)

    def run_all(self) -> list[JobResult]:
        return [result for result in (self.run(item.id) for item in self.definitions() if item.enabled) if result]

    def _finish(self, definition: JobDefinition, result: JobResult) -> JobResult:
        now = self.clock()
        with self._lock:
            state = self._states.setdefault(definition.id, JobState())
            state.last_run = now.isoformat(timespec="seconds")
            state.last_summary = result.summary
            state.runs += 1
            if result.ok:
                state.last_error = None
            else:
                state.failures += 1
                state.last_error = result.summary
            self._save_state()
        self._record(definition, result)
        if result.notify:
            self._notify(definition, result, now)
        return result

    def _record(self, definition: JobDefinition, result: JobResult) -> None:
        if self.audit is None:
            return
        try:
            self.audit.write(
                AuditEvent(
                    category=AuditCategory.SCHEDULE,
                    event=definition.job,
                    subject=definition.id,
                    source="schedule",
                    status="success" if result.ok else "failed",
                    message=result.summary,
                    error=None if result.ok else result.summary,
                    data=dict(result.data),
                )
            )
        except Exception as error:
            logger.warning("Could not record a scheduled run: %s", error)

    def _notify(self, definition: JobDefinition, result: JobResult, now: datetime) -> None:
        quiet = self.quiet_hours.active(now.astimezone() if now.tzinfo else now)
        notification = Notification(
            watcher_id=definition.id,
            title=definition.label,
            body=result.summary,
            kind=definition.job,
            created_at=now.isoformat(timespec="seconds"),
        )
        for channel in definition.channels:
            notifier = self.notifiers.get(channel)
            if notifier is None or (quiet and channel == "toast"):
                continue
            try:
                notifier.send(notification)
            except Exception as error:
                logger.warning("Notifier %s failed for %s: %s", channel, definition.label, error)

    def describe(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for definition in self.definitions():
            state = self.state(definition.id) or JobState()
            rows.append(
                {
                    "id": definition.id,
                    "job": definition.job,
                    "label": definition.label,
                    "schedule": definition.schedule,
                    "params": dict(definition.params),
                    "enabled": definition.enabled,
                    "runs": state.runs,
                    "failures": state.failures,
                    "last_run": state.last_run,
                    "last_summary": state.last_summary,
                    "last_error": state.last_error,
                }
            )
        return rows

    def kinds(self) -> list[str]:
        return sorted(self.jobs)


def now_iso() -> str:
    return utc_now_iso()
