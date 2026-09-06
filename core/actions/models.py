from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ConfirmationPreview:
    summary: str
    target: str | None = None
    after: str | None = None
    impact: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionRequest:
    action: str
    arguments: dict[str, Any]
    source: str = "command"
    reason: str = ""
    follow_up: ActionRequest | None = None
    workflow_goal: str | None = None
    workflow_parameters: dict[str, Any] | None = None


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    error: str | None = None
    requires_confirmation: bool = False
    resolved_target: str | None = None
    resolved_arguments: dict[str, Any] | None = None
    confirmation_preview: ConfirmationPreview | None = None


@dataclass(frozen=True)
class ActionResult:
    status: str
    message: str
    action: str
    resolved_target: str | None = None
    error: str | None = None
    confirmation_preview: ConfirmationPreview | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())


@dataclass(frozen=True)
class ApplicationConfig:
    id: str
    display_name: str
    executable: str
    aliases: list[str]

