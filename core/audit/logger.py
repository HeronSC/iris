# File: core/audit/logger.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.observability.request_context import current_request_id


class AuditLogger:
    def __init__(self, audit_folder: str | Path) -> None:
        self.audit_folder = Path(audit_folder).expanduser()
        self.audit_folder.mkdir(parents=True, exist_ok=True)
        self.log_path = self.audit_folder / "memory_changes.jsonl"

    def log(self, entry: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(entry)
        record.setdefault("request_id", current_request_id())
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_entries(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries
