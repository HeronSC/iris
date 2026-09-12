# File: core/audit/retention.py

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DEFAULT_KEEP_DAYS = 90
STAMP_KEYS = ("created_at", "timestamp", "time", "at")


@dataclass(frozen=True)
class RetentionReport:
    kept_days: int
    files: dict[str, tuple[int, int]] = field(default_factory=dict)
    captures_removed: int = 0

    @property
    def removed(self) -> int:
        return sum(before - after for before, after in self.files.values()) + self.captures_removed

    def summary(self) -> str:
        parts = [f"{name}: {before - after} of {before} lines older than {self.kept_days} days removed" for name, (before, after) in self.files.items() if before != after]
        if self.captures_removed:
            parts.append(f"{self.captures_removed} captured file{'s' if self.captures_removed != 1 else ''} removed")
        return "; ".join(parts) if parts else f"Nothing older than {self.kept_days} days to remove"


def _stamp_of(line: str) -> datetime | None:
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in STAMP_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def trim_jsonl(path: str | Path, *, keep_days: int = DEFAULT_KEEP_DAYS, now: datetime | None = None) -> tuple[int, int]:
    target = Path(path)
    if not target.is_file():
        return 0, 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(1, int(keep_days)))
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    kept: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        stamp = _stamp_of(line)
        if stamp is None or stamp >= cutoff:
            kept.append(line)
    if len(kept) == len(lines):
        return len(lines), len(lines)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    os.replace(temporary, target)
    return len(lines), len(kept)


def remove_old_files(folder: str | Path, *, keep_days: int = DEFAULT_KEEP_DAYS, suffixes: Iterable[str] = (".jpg", ".jpeg", ".png", ".mp4"), now: datetime | None = None) -> int:
    root = Path(folder)
    if not root.is_dir():
        return 0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=max(1, int(keep_days)))).timestamp()
    wanted = {item.lower() for item in suffixes}
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in wanted:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as error:
            logger.debug("Could not remove %s: %s", path, error)
    return removed


def apply_retention(files: Iterable[str | Path], *, keep_days: int = DEFAULT_KEEP_DAYS, capture_folders: Iterable[str | Path] = (), now: datetime | None = None) -> RetentionReport:
    report: dict[str, tuple[int, int]] = {}
    for path in files:
        target = Path(path)
        try:
            report[target.name] = trim_jsonl(target, keep_days=keep_days, now=now)
        except OSError as error:
            logger.warning("Retention skipped %s: %s", target, error)
    captures = sum(remove_old_files(folder, keep_days=keep_days, now=now) for folder in capture_folders)
    return RetentionReport(kept_days=int(keep_days), files=report, captures_removed=captures)


__all__ = ["DEFAULT_KEEP_DAYS", "RetentionReport", "apply_retention", "remove_old_files", "trim_jsonl"]
