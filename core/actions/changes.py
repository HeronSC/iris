# File: core/actions/changes.py

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from core.audit.stream import AuditCategory, AuditEvent, AuditStream
from core.observability.request_context import current_request_id

logger = logging.getLogger(__name__)

LEDGER_FILE_NAME = "changes.jsonl"

DEFAULT_KEEP = 50

MISSING = "missing"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ChangeRecord:
    id: str
    action: str
    created_at: str
    files: dict[str, str]
    request_id: str | None = None
    undone_at: str | None = None
    reason: str = ""

    @property
    def undone(self) -> bool:
        return self.undone_at is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "created_at": self.created_at,
            "files": dict(self.files),
            "request_id": self.request_id,
            "undone_at": self.undone_at,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ChangeRecord":
        files = payload.get("files")
        return cls(
            id=str(payload.get("id", "")),
            action=str(payload.get("action", "")),
            created_at=str(payload.get("created_at", "")),
            files={str(key): str(value) for key, value in files.items()} if isinstance(files, dict) else {},
            request_id=payload.get("request_id") or None,
            undone_at=payload.get("undone_at") or None,
            reason=str(payload.get("reason", "") or ""),
        )


@dataclass(frozen=True)
class UndoReport:
    change: ChangeRecord
    restored: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def summary(self) -> str:
        parts: list[str] = []
        if self.restored:
            parts.append(f"restored {', '.join(Path(item).name for item in self.restored)}")
        if self.removed:
            parts.append(f"removed {', '.join(Path(item).name for item in self.removed)}")
        text = f"Undid {self.change.action} from {self.change.created_at}: " + ("; ".join(parts) or "nothing to put back")
        if self.failures:
            text += "; failed: " + ", ".join(f"{Path(name).name} ({error})" for name, error in self.failures.items())
        return text


class ChangeLedger:
    def __init__(self, folder: str | Path, *, audit: AuditStream | None = None, keep: int = DEFAULT_KEEP) -> None:
        self.folder = Path(folder).expanduser()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.folder / LEDGER_FILE_NAME
        self.audit = audit
        self.keep = max(1, int(keep))
        self._lock = threading.Lock()

    def snapshot(self, action: str, paths: Iterable[str | Path], *, reason: str = "") -> ChangeRecord | None:
        wanted = [Path(item).expanduser() for item in paths if str(item).strip()]
        if not wanted:
            return None
        change_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"
        home = self.folder / change_id
        home.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        for index, path in enumerate(wanted):
            if path.exists() and path.is_file():
                copy = home / f"{index}-{path.name}"
                shutil.copy2(path, copy)
                files[str(path)] = str(copy)
            else:
                files[str(path)] = MISSING
        record = ChangeRecord(
            id=change_id,
            action=action,
            created_at=utc_now_iso(),
            files=files,
            request_id=current_request_id(),
            reason=reason,
        )
        with self._lock:
            self._append(record)
            self._prune()
        return record

    def records(self) -> list[ChangeRecord]:
        if not self.ledger_path.exists():
            return []
        found: dict[str, ChangeRecord] = {}
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("id"):
                found[str(payload["id"])] = ChangeRecord.from_json(payload)
        return list(found.values())

    def recent(self, limit: int = 10) -> list[ChangeRecord]:
        return self.records()[-limit:][::-1]

    def get(self, change_id: str) -> ChangeRecord | None:
        matches = [item for item in self.records() if item.id == change_id or item.id.startswith(change_id)]
        return matches[-1] if len(matches) == 1 or (matches and matches[-1].id == change_id) else None

    def undo(self, change_id: str | None = None) -> UndoReport | None:
        if change_id is None:
            candidates = [item for item in self.records() if not item.undone]
            change = candidates[-1] if candidates else None
        else:
            change = self.get(change_id)
        if change is None:
            return None
        if change.undone:
            return UndoReport(change=change, failures={"change": "already undone"})
        restored: list[str] = []
        removed: list[str] = []
        failures: dict[str, str] = {}
        for live, copy in change.files.items():
            target = Path(live)
            try:
                if copy == MISSING:
                    if target.exists():
                        target.unlink()
                        removed.append(live)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(copy, target)
                restored.append(live)
            except OSError as error:
                failures[live] = str(error)
        undone = ChangeRecord(**{**change.__dict__, "undone_at": utc_now_iso()})
        with self._lock:
            self._append(undone)
        report = UndoReport(change=undone, restored=tuple(restored), removed=tuple(removed), failures=failures)
        self._record(report)
        return report

    def _append(self, record: ChangeRecord) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")

    def _prune(self) -> None:
        records = self.records()
        excess = records[: max(0, len(records) - self.keep)]
        for record in excess:
            home = self.folder / record.id
            if home.exists():
                shutil.rmtree(home, ignore_errors=True)
        if excess:
            keep = records[len(excess):]
            self.ledger_path.write_text(
                "".join(json.dumps(item.to_json(), ensure_ascii=False) + "\n" for item in keep), encoding="utf-8"
            )

    def _record(self, report: UndoReport) -> None:
        if self.audit is None:
            return
        try:
            self.audit.write(
                AuditEvent(
                    category=AuditCategory.ACTION,
                    event="undo",
                    subject=report.change.action,
                    target=", ".join(report.change.files),
                    source="command",
                    status="success" if report.ok else "failed",
                    message=report.summary,
                    data={"change_id": report.change.id, "restored": list(report.restored), "removed": list(report.removed)},
                )
            )
        except Exception as error:
            logger.warning("Could not record an undo: %s", error)


__all__ = ["ChangeLedger", "ChangeRecord", "LEDGER_FILE_NAME", "MISSING", "UndoReport"]
