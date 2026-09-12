# File: core/workflows/models.py

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

ON_FAILURE = ("stop", "continue", "retry")

TRIGGER_KINDS = ("manual", "schedule", "watcher")

RUN_STATUSES = ("success", "failed", "partial", "awaiting_approval", "dry_run", "blocked")

DEFAULT_TIMEOUT_SECONDS = 120.0

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-]+)\s*\}\}")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    on_failure: str = "stop"
    retries: int = 0
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    approve: bool = False
    save_as: str | None = None

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise WorkflowError("A step needs an id")
        if not str(self.tool).strip():
            raise WorkflowError(f"Step {self.id} needs a tool")
        if self.on_failure not in ON_FAILURE:
            raise WorkflowError(f"Step {self.id}: on_failure is one of {', '.join(ON_FAILURE)}")
        if self.retries < 0 or self.retries > 10:
            raise WorkflowError(f"Step {self.id}: retries is 0 to 10")
        if self.timeout_seconds <= 0:
            raise WorkflowError(f"Step {self.id}: timeout_seconds must be positive")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WorkflowStep":
        known = {key: value for key, value in payload.items() if key in cls.__dataclass_fields__}
        known["arguments"] = dict(known.get("arguments") or {})
        return cls(**known)


@dataclass(frozen=True)
class WorkflowTrigger:
    kind: str = "manual"
    cron: str | None = None
    interval_seconds: int | None = None
    watcher: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in TRIGGER_KINDS:
            raise WorkflowError(f"A trigger is one of {', '.join(TRIGGER_KINDS)}")
        if self.kind == "schedule" and not (self.cron or self.interval_seconds):
            raise WorkflowError("A scheduled trigger needs cron or interval_seconds")
        if self.kind == "watcher" and not str(self.watcher or "").strip():
            raise WorkflowError("A watcher trigger names a watcher id or kind")

    def describe(self) -> str:
        if self.kind == "schedule":
            return f"cron {self.cron}" if self.cron else f"every {self.interval_seconds}s"
        if self.kind == "watcher":
            return f"when watcher {self.watcher} fires"
        return "manual"

    def to_json(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_json(cls, payload: Any) -> "WorkflowTrigger":
        if not isinstance(payload, dict):
            return cls()
        known = {key: value for key, value in payload.items() if key in cls.__dataclass_fields__}
        return cls(**known)


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    steps: tuple[WorkflowStep, ...]
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    description: str = ""
    trigger: WorkflowTrigger = field(default_factory=WorkflowTrigger)
    enabled: bool = True
    version: int = 1
    tool_versions: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise WorkflowError("A workflow needs a name")
        if not self.steps:
            raise WorkflowError(f"Workflow {self.name} has no steps")
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise WorkflowError(f"Workflow {self.name} repeats step id {step.id}")
            seen.add(step.id)

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(step.tool for step in self.steps))

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled,
            "trigger": self.trigger.to_json(),
            "steps": [step.to_json() for step in self.steps],
            "tool_versions": dict(self.tool_versions),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WorkflowDefinition":
        steps = payload.get("steps")
        if not isinstance(steps, list):
            raise WorkflowError("A workflow needs a steps list")
        return cls(
            id=str(payload.get("id") or uuid4().hex[:8]),
            name=str(payload.get("name", "")),
            description=str(payload.get("description", "") or ""),
            version=int(payload.get("version", 1)),
            enabled=bool(payload.get("enabled", True)),
            trigger=WorkflowTrigger.from_json(payload.get("trigger")),
            steps=tuple(WorkflowStep.from_json(item) for item in steps if isinstance(item, dict)),
            tool_versions={str(key): str(value) for key, value in (payload.get("tool_versions") or {}).items()},
            created_at=str(payload.get("created_at") or utc_now_iso()),
            updated_at=str(payload.get("updated_at") or utc_now_iso()),
        )


@dataclass(frozen=True)
class StepOutcome:
    step_id: str
    tool: str
    status: str
    message: str = ""
    error: str | None = None
    attempts: int = 1
    elapsed_ms: float = 0.0
    arguments: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    preview: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "StepOutcome":
        known = {key: value for key, value in payload.items() if key in cls.__dataclass_fields__}
        return cls(**known)


@dataclass(frozen=True)
class WorkflowRun:
    workflow_id: str
    workflow_name: str
    version: int
    status: str
    id: str = field(default_factory=lambda: uuid4().hex[:10])
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    trigger: str = "manual"
    payload: dict[str, Any] = field(default_factory=dict)
    steps: tuple[StepOutcome, ...] = ()
    next_step: str | None = None
    note: str = ""
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise WorkflowError(f"A run status is one of {', '.join(RUN_STATUSES)}")

    @property
    def awaiting(self) -> bool:
        return self.status == "awaiting_approval"

    @property
    def summary(self) -> str:
        done = sum(1 for item in self.steps if item.status == "success")
        text = f"{self.workflow_name} v{self.version}: {self.status} ({done}/{len(self.steps)} steps ok)"
        if self.note:
            text += f" — {self.note}"
        return text

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "version": self.version,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "trigger": self.trigger,
            "payload": dict(self.payload),
            "steps": [item.to_json() for item in self.steps],
            "next_step": self.next_step,
            "note": self.note,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WorkflowRun":
        steps = payload.get("steps")
        return cls(
            id=str(payload.get("id", "")),
            workflow_id=str(payload.get("workflow_id", "")),
            workflow_name=str(payload.get("workflow_name", "")),
            version=int(payload.get("version", 1)),
            status=str(payload.get("status", "failed")),
            started_at=str(payload.get("started_at") or utc_now_iso()),
            finished_at=payload.get("finished_at") or None,
            trigger=str(payload.get("trigger") or "manual"),
            payload=dict(payload.get("payload") or {}),
            steps=tuple(StepOutcome.from_json(item) for item in steps if isinstance(item, dict)) if isinstance(steps, list) else (),
            next_step=payload.get("next_step") or None,
            note=str(payload.get("note") or ""),
            dry_run=bool(payload.get("dry_run", False)),
        )


def _lookup(context: dict[str, Any], dotted: str) -> Any:
    current: Any = context
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, (list, tuple)) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise WorkflowError(f"Nothing called {dotted} is available to this step")
    return current


def render_arguments(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    def render(value: Any) -> Any:
        if isinstance(value, str):
            whole = _PLACEHOLDER.fullmatch(value.strip())
            if whole:
                return _lookup(context, whole.group(1))
            return _PLACEHOLDER.sub(lambda match: str(_lookup(context, match.group(1))), value)
        if isinstance(value, dict):
            return {key: render(item) for key, item in value.items()}
        if isinstance(value, list):
            return [render(item) for item in value]
        return value

    return {key: render(item) for key, item in arguments.items()}


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ON_FAILURE",
    "RUN_STATUSES",
    "StepOutcome",
    "TRIGGER_KINDS",
    "WorkflowDefinition",
    "WorkflowError",
    "WorkflowRun",
    "WorkflowStep",
    "WorkflowTrigger",
    "render_arguments",
    "utc_now_iso",
]
