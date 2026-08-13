from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ActionAuditLogger:
    def __init__(self, audit_folder: str | Path) -> None:
        self.audit_folder = Path(audit_folder).expanduser()
        self.audit_folder.mkdir(parents=True, exist_ok=True)
        self.log_path = self.audit_folder / "actions.jsonl"

    def log(self, entry: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        selected = lines[-limit:]
        entries: list[dict[str, Any]] = []
        for line in selected:
            if line.strip():
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
        return entries
