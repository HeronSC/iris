# File: core/context/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process_name: str
    pid: int
    exe_path: str | None = None

    @property
    def process_key(self) -> str:
        return self.process_name.lower()


@dataclass(frozen=True)
class ActiveContext:
    app: str
    title: str
    provider: str
    captured_at: str = field(default_factory=utc_now_iso)
    target: str | None = None
    target_kind: str | None = None
    project: str | None = None
    selection: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str | None, str | None]:
        return (self.app.lower(), self.title, self.target, self.selection)

    def describe(self) -> str:
        parts = [self.app]
        if self.target:
            label = self.target_kind or "target"
            parts.append(f"{label} {self.target}")
        elif self.title:
            parts.append(f'"{self.title}"')
        if self.project:
            parts.append(f"in {self.project}")
        if self.selection:
            parts.append(f"selection {self.selection}")
        return ", ".join(parts)

    @property
    def target_name(self) -> str | None:
        if not self.target:
            return None
        return PurePath(self.target).name or self.target

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "app": self.app,
            "title": self.title,
            "provider": self.provider,
            "captured_at": self.captured_at,
        }
        for name in ("target", "target_kind", "project", "selection"):
            value = getattr(self, name)
            if value:
                payload[name] = value
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload


__all__ = ["ActiveContext", "WindowInfo", "utc_now_iso"]
