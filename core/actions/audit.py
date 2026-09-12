# File: core/actions/audit.py

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.audit.stream import AuditCategory, AuditEvent, AuditStream

HISTORY_FILE_NAME = "actions.jsonl"


class ActionAuditLogger:
    def __init__(self, audit_folder: str | Path) -> None:
        self.stream = AuditStream(audit_folder)
        self.audit_folder = self.stream.folder
        self.log_path = self.stream.path

    def log(self, entry: dict[str, Any]) -> None:
        payload = dict(entry)
        action = str(payload.pop("action", "") or "")
        tool = str(payload.pop("tool", "") or action)
        recorded_at = payload.pop("created_at", None)
        self.stream.write(
            AuditEvent(
                category=AuditCategory.ACTION,
                event=tool,
                subject=action,
                target=_text(payload.pop("resolved_target", None)),
                source=_text(payload.pop("source", None)),
                status=_text(payload.pop("status", None)),
                message=_text(payload.pop("message", None)),
                error=_text(payload.pop("error", None)),
                request_id=payload.pop("request_id", None),
                data=payload,
                **({"created_at": str(recorded_at)} if recorded_at else {}),
            )
        )

    def read_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        entries = self.stream.read_history(HISTORY_FILE_NAME)
        entries.extend(_entry_shape(event) for event in self.stream.read(category=AuditCategory.ACTION))
        return entries[-limit:] if limit >= 0 else entries


def _entry_shape(event: AuditEvent) -> dict[str, Any]:
    entry = dict(event.data)
    entry["action"] = event.subject or event.event
    entry["tool"] = event.event
    entry["resolved_target"] = event.target
    entry["source"] = event.source
    entry["status"] = event.status
    entry["message"] = event.message
    entry["error"] = event.error
    entry["request_id"] = event.request_id
    entry["created_at"] = event.created_at
    return entry


def _text(value: Any) -> str | None:
    return None if value is None else str(value)
