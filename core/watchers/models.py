# File: core/watchers/models.py

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_RENOTIFY_MINUTES = 60.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)


def parse_duration(text: str | int | float) -> int:
    if isinstance(text, (int, float)):
        return max(1, int(text))
    match = _DURATION.match(str(text))
    if match is None:
        raise ValueError(f"Not a duration: {text!r} (use 30s, 5m, 2h, 1d)")
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return max(1, int(value * factor))


def format_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


@dataclass(frozen=True)
class WatcherContext:
    knowledge: Any = None
    cameras: Any = None


@dataclass(frozen=True)
class WatcherDefinition:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    name: str = ""
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    channels: tuple[str, ...] = ("toast", "inbox")
    enabled: bool = True
    renotify_minutes: float = DEFAULT_RENOTIFY_MINUTES
    notify_on_clear: bool = True
    created_at: str = field(default_factory=utc_now_iso)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["channels"] = list(self.channels)
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WatcherDefinition":
        channels = payload.get("channels") or ["toast", "inbox"]
        return cls(
            kind=str(payload["kind"]),
            params=dict(payload.get("params") or {}),
            id=str(payload.get("id") or uuid4().hex[:8]),
            name=str(payload.get("name") or ""),
            interval_seconds=int(payload.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS),
            channels=tuple(str(item) for item in channels),
            enabled=bool(payload.get("enabled", True)),
            renotify_minutes=float(payload.get("renotify_minutes", DEFAULT_RENOTIFY_MINUTES)),
            notify_on_clear=bool(payload.get("notify_on_clear", True)),
            created_at=str(payload.get("created_at") or utc_now_iso()),
        )

    @property
    def label(self) -> str:
        return self.name or f"{self.kind} {self._params_text()}".strip()

    def _params_text(self) -> str:
        return " ".join(f"{key}={value}" for key, value in self.params.items())


@dataclass
class WatcherState:
    active: bool = False
    last_checked: str | None = None
    last_summary: str = ""
    last_notified: str | None = None
    last_value: Any = None
    baseline: Any = None
    failures: int = 0
    last_error: str | None = None
    notifications: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WatcherState":
        return cls(**{key: payload.get(key, default) for key, default in asdict(cls()).items()})


@dataclass(frozen=True)
class Notification:
    watcher_id: str
    title: str
    body: str
    kind: str
    created_at: str = field(default_factory=utc_now_iso)
    cleared: bool = False
    delivered_to: tuple[str, ...] = ()
    deferred: bool = False

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["delivered_to"] = list(self.delivered_to)
        return payload
