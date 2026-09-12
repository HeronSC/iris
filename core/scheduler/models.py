# File: core/scheduler/models.py

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

DEFAULT_INTERVAL_SECONDS = 3600


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class JobResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    notify: bool = False


@dataclass(frozen=True)
class JobDefinition:
    job: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    name: str = ""
    interval_seconds: int | None = DEFAULT_INTERVAL_SECONDS
    cron: str | None = None
    channels: tuple[str, ...] = ("toast", "inbox")
    enabled: bool = True
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not str(self.job).strip():
            raise ValueError("A scheduled job needs a job name")
        if self.cron is None and not self.interval_seconds:
            raise ValueError(f"{self.job} needs either an interval or a cron expression")
        if self.cron is not None and len(str(self.cron).split()) != 5:
            raise ValueError(
                f"A cron expression has five fields (minute hour day month day-of-week), got {self.cron!r}"
            )

    @property
    def label(self) -> str:
        return self.name or self.job

    @property
    def schedule(self) -> str:
        if self.cron:
            return f"cron {self.cron}"
        seconds = int(self.interval_seconds or 0)
        if seconds % 86400 == 0:
            return f"every {seconds // 86400}d"
        if seconds % 3600 == 0:
            return f"every {seconds // 3600}h"
        if seconds % 60 == 0:
            return f"every {seconds // 60}m"
        return f"every {seconds}s"

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["channels"] = list(self.channels)
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "JobDefinition":
        fields = dict(payload)
        channels = fields.get("channels")
        fields["channels"] = tuple(str(item) for item in channels) if isinstance(channels, list) else ("toast", "inbox")
        fields["params"] = dict(fields.get("params") or {})
        known = {key: value for key, value in fields.items() if key in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class JobState:
    last_run: str | None = None
    last_summary: str | None = None
    last_error: str | None = None
    runs: int = 0
    failures: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "JobState":
        known = {key: value for key, value in payload.items() if key in cls.__dataclass_fields__}
        return cls(**known)


__all__ = ["DEFAULT_INTERVAL_SECONDS", "JobDefinition", "JobResult", "JobState", "utc_now_iso"]
