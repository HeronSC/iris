# File: core/storage/backups.py

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.storage.sqlite_database import SQLiteDatabase, remove_sidecars

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 7

RUN_STAMP = "%Y%m%dT%H%M%SZ"

SKIP_IN_FOLDERS = ("*.db", "*.db-wal", "*.db-shm", "__pycache__")


@dataclass(frozen=True)
class BackupReport:
    run: Path
    databases: dict[str, Path] = field(default_factory=dict)
    folders: dict[str, Path] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    pruned: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def summary(self) -> str:
        parts = [f"{len(self.databases)} database(s) and {len(self.folders)} folder(s) copied to {self.run.name}"]
        if self.pruned:
            parts.append(f"{len(self.pruned)} older run(s) removed")
        if self.failures:
            parts.append("failed: " + ", ".join(f"{name} ({error})" for name, error in self.failures.items()))
        return "; ".join(parts)


@dataclass(frozen=True)
class RestoreReport:
    run: Path
    restored: tuple[str, ...]
    set_aside: dict[str, Path]
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def summary(self) -> str:
        text = f"Restored {', '.join(self.restored) or 'nothing'} from {self.run.name}"
        if self.set_aside:
            text += f"; the previous copies are beside them as .before-restore"
        if self.failures:
            text += "; failed: " + ", ".join(f"{name} ({error})" for name, error in self.failures.items())
        return text


class BackupService:
    def __init__(
        self,
        destination: str | Path,
        *,
        databases: dict[str, SQLiteDatabase] | None = None,
        folders: dict[str, Path] | None = None,
        keep: int = DEFAULT_KEEP,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.destination = Path(destination).expanduser()
        self.databases = dict(databases or {})
        self.folders = {name: Path(path) for name, path in (folders or {}).items()}
        self.keep = max(1, int(keep))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self) -> BackupReport:
        run = self.destination / self.clock().strftime(RUN_STAMP)
        run.mkdir(parents=True, exist_ok=True)
        databases: dict[str, Path] = {}
        folders: dict[str, Path] = {}
        failures: dict[str, str] = {}
        for name, database in self.databases.items():
            if not database.db_path.exists():
                continue
            try:
                copy = database.backup_to(run / f"{name}.db")
                report = SQLiteDatabase(copy).verify()
                if not report.ok:
                    raise RuntimeError(f"copy failed quick_check: {report.summary}")
                databases[name] = copy
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                failures[name] = str(error)
                logger.warning("Backup of %s failed: %s", name, error)
        for name, folder in self.folders.items():
            if not folder.exists():
                continue
            try:
                target = run / name
                shutil.copytree(folder, target, ignore=shutil.ignore_patterns(*SKIP_IN_FOLDERS), dirs_exist_ok=True)
                folders[name] = target
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                failures[name] = str(error)
                logger.warning("Backup of %s failed: %s", name, error)
        pruned = self._prune(keep_run=run)
        return BackupReport(run=run, databases=databases, folders=folders, failures=failures, pruned=pruned)

    def runs(self) -> list[Path]:
        if not self.destination.exists():
            return []
        return sorted(item for item in self.destination.iterdir() if item.is_dir() and not item.name.startswith("."))

    def latest(self) -> Path | None:
        found = self.runs()
        return found[-1] if found else None

    def _prune(self, *, keep_run: Path) -> tuple[str, ...]:
        runs = [item for item in self.runs() if item != keep_run]
        excess = len(runs) + 1 - self.keep
        removed: list[str] = []
        for item in runs[: max(0, excess)]:
            try:
                shutil.rmtree(item)
                removed.append(item.name)
            except OSError as error:
                logger.warning("Could not remove old backup %s: %s", item, error)
        return tuple(removed)

    def restore(self, run: str | Path | None = None, *, only: tuple[str, ...] | None = None) -> RestoreReport:
        source = self._resolve_run(run)
        stamp = self.clock().strftime(RUN_STAMP)
        restored: list[str] = []
        set_aside: dict[str, Path] = {}
        failures: dict[str, str] = {}
        wanted = set(only) if only else None
        for name, database in self.databases.items():
            copy = source / f"{name}.db"
            if not copy.exists() or (wanted is not None and name not in wanted):
                continue
            try:
                if not SQLiteDatabase(copy).verify().ok:
                    raise RuntimeError("the backup copy itself fails quick_check")
                live = database.db_path
                if live.exists():
                    aside = live.with_name(f"{live.name}.before-restore-{stamp}")
                    shutil.move(str(live), str(aside))
                    set_aside[name] = aside
                remove_sidecars(live)
                shutil.copy2(copy, live)
                database.verify(force=True)
                restored.append(name)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                failures[name] = str(error)
                logger.warning("Restore of %s failed: %s", name, error)
        for name, folder in self.folders.items():
            copy = source / name
            if not copy.exists() or (wanted is not None and name not in wanted):
                continue
            try:
                if folder.exists():
                    aside = folder.with_name(f"{folder.name}.before-restore-{stamp}")
                    shutil.move(str(folder), str(aside))
                    set_aside[name] = aside
                shutil.copytree(copy, folder)
                restored.append(name)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                failures[name] = str(error)
                logger.warning("Restore of %s failed: %s", name, error)
        return RestoreReport(run=source, restored=tuple(restored), set_aside=set_aside, failures=failures)

    def _resolve_run(self, run: str | Path | None) -> Path:
        if run is None:
            latest = self.latest()
            if latest is None:
                raise FileNotFoundError(f"No backups under {self.destination}")
            return latest
        candidate = Path(run)
        if candidate.is_dir():
            return candidate
        matches = [item for item in self.runs() if item.name.startswith(str(run))]
        if len(matches) != 1:
            raise FileNotFoundError(f"No single backup run matches {run}")
        return matches[0]

    def describe(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.runs():
            files = [entry for entry in item.rglob("*") if entry.is_file()]
            rows.append(
                {
                    "run": item.name,
                    "databases": sorted(entry.stem for entry in item.glob("*.db")),
                    "folders": sorted(entry.name for entry in item.iterdir() if entry.is_dir()),
                    "size_bytes": sum(entry.stat().st_size for entry in files),
                }
            )
        return rows


__all__ = ["BackupReport", "BackupService", "DEFAULT_KEEP", "RestoreReport"]
