# File: core/audit/logger.py

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.audit.stream import AuditCategory, AuditEvent, AuditStream

HISTORY_FILE_NAME = "memory_changes.jsonl"

CANONICAL_BY_ENTRY_KEY = {
    "action": "event",
    "subject": "subject",
    "actor": "actor",
    "status": "status",
    "error": "error",
}


class AuditLogger:
    def __init__(self, audit_folder: str | Path) -> None:
        self.stream = AuditStream(audit_folder)
        self.audit_folder = self.stream.folder
        self.log_path = self.stream.path

    def log(self, entry: dict[str, Any]) -> None:
        payload = dict(entry)
        fields = {
            canonical: payload.pop(key)
            for key, canonical in CANONICAL_BY_ENTRY_KEY.items()
            if key in payload
        }
        recorded_at = payload.get("timestamp")
        self.stream.write(
            AuditEvent(
                category=AuditCategory.MEMORY,
                event=str(fields.pop("event", "changed")),
                request_id=payload.pop("request_id", None),
                data=payload,
                **{key: _text(value) for key, value in fields.items()},
                **({"created_at": str(recorded_at)} if recorded_at else {}),
            )
        )

    def read_entries(self) -> list[dict[str, Any]]:
        entries = self.stream.read_history(HISTORY_FILE_NAME)
        entries.extend(_entry_shape(event) for event in self.stream.read(category=AuditCategory.MEMORY))
        return entries


def _entry_shape(event: AuditEvent) -> dict[str, Any]:
    entry = dict(event.data)
    entry["action"] = event.event
    for key, canonical in CANONICAL_BY_ENTRY_KEY.items():
        value = getattr(event, canonical)
        if key != "action" and value is not None:
            entry[key] = value
    entry.setdefault("timestamp", event.created_at)
    entry["created_at"] = event.created_at
    entry["request_id"] = event.request_id
    return entry


def _text(value: Any) -> str | None:
    return None if value is None else str(value)
