# File: core/audit/stream.py

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from core.audit.redaction import redact
from core.observability.request_context import current_request_id

AUDIT_FILE_NAME = "audit.jsonl"

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path).lower()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class AuditCategory(str, Enum):
    MEMORY = "memory"
    ACTION = "action"
    TOOL = "tool"
    PERMISSION = "permission"
    SCHEDULE = "schedule"


CANONICAL_FIELDS = ("subject", "target", "actor", "source", "status", "message", "error")


@dataclass(frozen=True)
class AuditEvent:
    category: AuditCategory
    event: str
    subject: str | None = None
    target: str | None = None
    actor: str | None = None
    source: str | None = None
    status: str | None = None
    message: str | None = None
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "created_at": self.created_at,
            "request_id": self.request_id,
            "category": self.category.value,
            "event": self.event,
        }
        for name in CANONICAL_FIELDS:
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        if self.data:
            payload["data"] = self.data
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "AuditEvent":
        raw_category = str(payload.get("category", "")).strip().lower()
        try:
            category = AuditCategory(raw_category)
        except ValueError:
            category = AuditCategory.TOOL
        data = payload.get("data")
        return cls(
            category=category,
            event=str(payload.get("event", "")),
            data=dict(data) if isinstance(data, dict) else {},
            request_id=_optional_str(payload.get("request_id")),
            created_at=str(payload.get("created_at") or utc_now_iso()),
            **{name: _optional_str(payload.get(name)) for name in CANONICAL_FIELDS},
        )

    def flatten(self) -> dict[str, Any]:
        merged = dict(self.data)
        merged.update({key: value for key, value in self.to_json().items() if key != "data"})
        return merged


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


class AuditStream:
    def __init__(self, folder: str | Path, file_name: str = AUDIT_FILE_NAME) -> None:
        self.folder = Path(folder).expanduser()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / file_name
        self._lock = _lock_for(self.path)

    def write(self, event: AuditEvent) -> AuditEvent:
        stored = AuditEvent(
            category=event.category,
            event=event.event,
            subject=event.subject,
            target=event.target,
            actor=event.actor,
            source=event.source,
            status=event.status,
            message=redact(event.message) if event.message else event.message,
            error=event.error,
            data=redact(dict(event.data)),
            request_id=event.request_id or current_request_id(),
            created_at=event.created_at,
        )
        line = json.dumps(stored.to_json(), ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return stored

    def record(self, category: AuditCategory, event: str, **fields: Any) -> AuditEvent:
        return self.write(AuditEvent(category=category, event=event, **fields))

    def read(
        self,
        *,
        category: AuditCategory | Iterable[AuditCategory] | None = None,
        request_id: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEvent]:
        wanted: set[str] | None = None
        if category is not None:
            items = [category] if isinstance(category, AuditCategory) else list(category)
            wanted = {item.value for item in items}

        events: list[AuditEvent] = []
        for payload in self._rows(self.path):
            if wanted is not None and str(payload.get("category", "")) not in wanted:
                continue
            if request_id is not None and str(payload.get("request_id") or "") != request_id:
                continue
            events.append(AuditEvent.from_json(payload))
        if limit is not None and limit >= 0:
            events = events[-limit:] if limit else []
        return events

    def read_history(self, file_name: str) -> list[dict[str, Any]]:
        return self._rows(self.folder / file_name)

    @staticmethod
    def _rows(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows


__all__ = ["AUDIT_FILE_NAME", "AuditCategory", "AuditEvent", "AuditStream", "utc_now_iso"]
