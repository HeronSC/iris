# File: core/workflows/service.py

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from core.audit.stream import AuditCategory, AuditEvent, AuditStream
from core.workflows.models import WorkflowDefinition, WorkflowError, WorkflowRun, utc_now_iso
from core.workflows.runner import WorkflowRunner

logger = logging.getLogger(__name__)

RUNS_FILE_NAME = "runs.jsonl"

DEFAULT_KEEP_RUNS = 200


class WorkflowService:
    def __init__(
        self,
        folder: str | Path,
        runner: WorkflowRunner,
        *,
        tool_version: Callable[[str], str | None] | None = None,
        audit: AuditStream | None = None,
        notify: Callable[[WorkflowRun], None] | None = None,
        keep_runs: int = DEFAULT_KEEP_RUNS,
    ) -> None:
        self.folder = Path(folder).expanduser()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.runs_path = self.folder / RUNS_FILE_NAME
        self.runner = runner
        self.tool_version = tool_version
        self.audit = audit
        self.notify = notify
        self.keep_runs = max(1, int(keep_runs))
        self._definitions: dict[str, WorkflowDefinition] = {}
        self._stamps: dict[str, tuple[float, int]] = {}
        self._lock = threading.RLock()
        self.halted = False
        self.load()

    def load(self) -> list[str]:
        problems: list[str] = []
        with self._lock:
            found: dict[str, WorkflowDefinition] = {}
            for path in sorted(self.folder.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    definition = WorkflowDefinition.from_json(payload)
                except (OSError, ValueError, WorkflowError) as error:
                    problems.append(f"{path.name}: {error}")
                    logger.warning("Skipping workflow %s: %s", path.name, error)
                    continue
                found[definition.id] = definition
                stat = path.stat()
                self._stamps[definition.id] = (stat.st_mtime, stat.st_size)
            self._definitions = found
        return problems

    def reload_if_changed(self) -> bool:
        current: dict[str, tuple[float, int]] = {}
        for path in self.folder.glob("*.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            current[path.stem] = (stat.st_mtime, stat.st_size)
        known = {self._path_for(item).stem: stamp for item, stamp in ((d, self._stamps.get(d.id)) for d in self._definitions.values())}
        if current == known:
            return False
        self.load()
        return True

    def _path_for(self, definition: WorkflowDefinition) -> Path:
        return self.folder / f"{definition.id}.json"

    def save(self, definition: WorkflowDefinition, *, bump: bool = True) -> WorkflowDefinition:
        with self._lock:
            existing = self._definitions.get(definition.id)
            version = definition.version
            if existing is not None and bump and existing.to_json() != definition.to_json():
                version = existing.version + 1
            pinned = self._pin(definition)
            stored = WorkflowDefinition(**{**definition.__dict__, "version": version, "tool_versions": pinned, "updated_at": utc_now_iso()})
            path = self._path_for(stored)
            path.write_text(json.dumps(stored.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self._definitions[stored.id] = stored
            stat = path.stat()
            self._stamps[stored.id] = (stat.st_mtime, stat.st_size)
            return stored

    def _pin(self, definition: WorkflowDefinition) -> dict[str, str]:
        if self.tool_version is None:
            return dict(definition.tool_versions)
        pinned: dict[str, str] = {}
        for tool in definition.tools:
            version = self.tool_version(tool)
            if version is not None:
                pinned[tool] = version
        return pinned

    def rebase(self, workflow_id: str) -> WorkflowDefinition | None:
        definition = self.get(workflow_id)
        if definition is None:
            return None
        return self.save(WorkflowDefinition(**{**definition.__dict__, "tool_versions": {}}), bump=True)

    def remove(self, workflow_id: str) -> WorkflowDefinition | None:
        definition = self.get(workflow_id)
        if definition is None:
            return None
        with self._lock:
            self._definitions.pop(definition.id, None)
            self._stamps.pop(definition.id, None)
            path = self._path_for(definition)
            if path.exists():
                path.unlink()
        return definition

    def set_enabled(self, workflow_id: str, enabled: bool) -> WorkflowDefinition | None:
        definition = self.get(workflow_id)
        if definition is None:
            return None
        return self.save(WorkflowDefinition(**{**definition.__dict__, "enabled": enabled}), bump=False)

    def definitions(self) -> list[WorkflowDefinition]:
        with self._lock:
            return sorted(self._definitions.values(), key=lambda item: item.name.lower())

    def get(self, workflow_id: str) -> WorkflowDefinition | None:
        with self._lock:
            if workflow_id in self._definitions:
                return self._definitions[workflow_id]
            lowered = workflow_id.strip().lower()
            matches = [
                item for key, item in self._definitions.items() if key.startswith(lowered) or item.name.lower() == lowered
            ]
            return matches[0] if len(matches) == 1 else None

    def for_watcher(self, watcher_id: str, kind: str) -> list[WorkflowDefinition]:
        return [
            item
            for item in self.definitions()
            if item.enabled and item.trigger.kind == "watcher" and item.trigger.watcher in {watcher_id, kind}
        ]

    def scheduled(self) -> list[WorkflowDefinition]:
        return [item for item in self.definitions() if item.trigger.kind == "schedule"]

    def run(self, workflow_id: str, *, payload: dict[str, Any] | None = None, trigger: str = "manual", dry_run: bool = False) -> WorkflowRun | None:
        self.reload_if_changed()
        definition = self.get(workflow_id)
        if definition is None:
            return None
        if self.halted and not dry_run:
            return self._finish(
                WorkflowRun(
                    workflow_id=definition.id,
                    workflow_name=definition.name,
                    version=definition.version,
                    status="blocked",
                    trigger=trigger,
                    payload=dict(payload or {}),
                    finished_at=utc_now_iso(),
                    note="Iris is stopped; /resume to continue",
                )
            )
        if not definition.enabled and not dry_run and trigger != "manual":
            return None
        run = self.runner.run(definition, payload=payload, trigger=trigger, dry_run=dry_run)
        return self._finish(run)

    def approve(self, run_id: str) -> WorkflowRun | None:
        run = self.get_run(run_id)
        if run is None or not run.awaiting:
            return None
        definition = self.get(run.workflow_id)
        if definition is None:
            return None
        resumed = self.runner.run(definition, payload=run.payload, trigger=run.trigger, resume=run, approved=True)
        return self._finish(resumed)

    def runs(self, limit: int = 20, *, workflow_id: str | None = None) -> list[WorkflowRun]:
        found: dict[str, WorkflowRun] = {}
        if self.runs_path.exists():
            try:
                lines = self.runs_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("id"):
                    run = WorkflowRun.from_json(payload)
                    found[run.id] = run
        rows = list(found.values())
        if workflow_id is not None:
            rows = [item for item in rows if item.workflow_id == workflow_id]
        return rows[-limit:][::-1]

    def get_run(self, run_id: str) -> WorkflowRun | None:
        matches = [item for item in self.runs(limit=self.keep_runs) if item.id == run_id or item.id.startswith(run_id)]
        return matches[0] if len(matches) == 1 or (matches and matches[0].id == run_id) else None

    def awaiting(self) -> list[WorkflowRun]:
        return [item for item in self.runs(limit=self.keep_runs) if item.awaiting]

    def _finish(self, run: WorkflowRun) -> WorkflowRun:
        with self._lock:
            self.runs_path.parent.mkdir(parents=True, exist_ok=True)
            with self.runs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(run.to_json(), ensure_ascii=False) + "\n")
            self._prune_runs()
        self._record(run)
        if self.notify is not None and not run.dry_run and run.status in {"failed", "partial", "awaiting_approval", "blocked"}:
            try:
                self.notify(run)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Workflow notification failed: %s", error)
        return run

    def _prune_runs(self) -> None:
        if not self.runs_path.exists():
            return
        lines = self.runs_path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= self.keep_runs * 2:
            return
        self.runs_path.write_text("\n".join(lines[-self.keep_runs :]) + "\n", encoding="utf-8")

    def _record(self, run: WorkflowRun) -> None:
        if self.audit is None:
            return
        try:
            self.audit.write(
                AuditEvent(
                    category=AuditCategory.SCHEDULE if run.trigger != "manual" else AuditCategory.ACTION,
                    event="workflow",
                    subject=run.workflow_name,
                    target=run.id,
                    source=f"workflow:{run.trigger}",
                    status=run.status,
                    message=run.summary,
                    data={"workflow_id": run.workflow_id, "version": run.version, "steps": [item.status for item in run.steps], "dry_run": run.dry_run},
                )
            )
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.warning("Could not record a workflow run: %s", error)

    def sync_schedules(self, schedules: Any) -> list[str]:
        #! @allow-local-import
        from core.scheduler.jobs import WORKFLOW_RUN
        #! @allow-local-import
        from core.scheduler.models import JobDefinition

        if WORKFLOW_RUN not in getattr(schedules, "jobs", {}):
            return []
        wanted: dict[str, WorkflowDefinition] = {f"workflow-{item.id}": item for item in self.scheduled()}
        touched: list[str] = []
        for job in list(schedules.definitions()):
            if job.job != WORKFLOW_RUN:
                continue
            if job.id not in wanted:
                schedules.remove(job.id)
                touched.append(f"removed {job.id}")
        for job_id, definition in wanted.items():
            existing = schedules.get(job_id)
            desired = JobDefinition(
                job=WORKFLOW_RUN,
                params={"workflow_id": definition.id},
                id=job_id,
                name=f"Workflow: {definition.name}",
                interval_seconds=definition.trigger.interval_seconds if not definition.trigger.cron else None,
                cron=definition.trigger.cron,
                channels=("inbox", "log"),
                enabled=definition.enabled,
            )
            if existing is None:
                schedules.add(desired)
                touched.append(f"added {job_id}")
            elif (existing.cron, existing.interval_seconds, existing.enabled) != (desired.cron, desired.interval_seconds, desired.enabled):
                schedules.remove(job_id)
                schedules.add(desired)
                touched.append(f"updated {job_id}")
        return touched

    def watcher_listener(self) -> Callable[[Any], None]:
        def on_notification(notification: Any) -> None:
            if getattr(notification, "cleared", False):
                return
            watcher_id = str(getattr(notification, "watcher_id", "") or "")
            kind = str(getattr(notification, "kind", "") or "")
            for definition in self.for_watcher(watcher_id, kind):
                payload = {
                    "watcher_id": watcher_id,
                    "kind": kind,
                    "title": getattr(notification, "title", ""),
                    "body": getattr(notification, "body", ""),
                    "created_at": getattr(notification, "created_at", ""),
                }
                self.run(definition.id, payload=payload, trigger="watcher")

        return on_notification

    def describe(self) -> list[dict[str, Any]]:
        self.reload_if_changed()
        rows: list[dict[str, Any]] = []
        for definition in self.definitions():
            last = self.runs(limit=1, workflow_id=definition.id)
            rows.append(
                {
                    "id": definition.id,
                    "name": definition.name,
                    "version": definition.version,
                    "enabled": definition.enabled,
                    "trigger": definition.trigger.describe(),
                    "steps": [f"{step.id}:{step.tool}" for step in definition.steps],
                    "drift": {tool: list(pair) for tool, pair in self.runner.version_drift(definition).items()},
                    "last_run": last[0].summary if last else None,
                }
            )
        return rows


__all__ = ["DEFAULT_KEEP_RUNS", "RUNS_FILE_NAME", "WorkflowService"]
