# File: core/excel/__init__.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from core.excel.closed import ClosedExcel, WorkbookUnavailable
from core.excel.live import LiveExcel, excel_application
from core.excel.models import CellHit, RangeData, SheetInfo, WorkbookInfo

logger = logging.getLogger(__name__)

NO_WORKBOOK = "No workbook is in view. Open one in Excel, or say which file."


@dataclass(frozen=True)
class Target:
    live: bool
    path: str
    handle: Any = None

    @property
    def name(self) -> str:
        return Path(self.path).name or self.path


class ExcelService:
    def __init__(self, allowed_roots: Iterable[str | Path] = (), *, live: LiveExcel | None = None, closed: ClosedExcel | None = None, context_service: Any = None) -> None:
        self.allowed_roots = [Path(item) for item in allowed_roots]
        self.live = live or LiveExcel(excel_application)
        self.closed = closed or ClosedExcel()
        self.context_service = context_service

    def within_roots(self, path: str | Path) -> bool:
        if not self.allowed_roots:
            return True
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        for root in self.allowed_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except (OSError, ValueError):
                continue
        return False

    def _context_workbook(self) -> str | None:
        service = self.context_service
        if service is None or getattr(service, "paused", False):
            return None
        try:
            current = service.current()
        except Exception:
            return None
        if current is None or getattr(current, "target_kind", None) != "workbook":
            return None
        return str(current.target) if current.target else None

    def resolve(self, reference: str | None) -> Target:
        wanted = (reference or "").strip().strip('"')
        if not wanted:
            wanted = self._context_workbook() or ""
        handle = self.live.workbook(wanted or None)
        if handle is not None:
            return Target(live=True, path=str(handle.FullName), handle=handle)
        if not wanted:
            raise WorkbookUnavailable(NO_WORKBOOK)
        path = Path(wanted).expanduser()
        if not path.is_absolute():
            raise WorkbookUnavailable(f"{wanted} is not open in Excel; give the full path to read it from disk.")
        if not self.within_roots(path):
            raise WorkbookUnavailable(f"{path} is outside the folders Iris may read.")
        if not path.is_file():
            raise WorkbookUnavailable(f"{path} does not exist.")
        return Target(live=False, path=str(path))

    def describe(self, target: Target) -> WorkbookInfo:
        if target.live:
            return self.live.describe(target.handle)
        return self.closed.describe(Path(target.path))

    def read(self, target: Target, sheet: str | None, address: str | None, *, formulas: bool = False, max_rows: int = 100) -> RangeData:
        if target.live:
            return self.live.read(target.handle, sheet, address, formulas=formulas, max_rows=max_rows)
        return self.closed.read(Path(target.path), sheet, address, formulas=formulas, max_rows=max_rows)

    def find(self, target: Target, text: str, sheet: str | None = None, *, limit: int = 50) -> list[CellHit]:
        if target.live:
            return self.live.find(target.handle, text, sheet, limit=limit)
        return self.closed.find(Path(target.path), text, sheet, limit=limit)

    def errors(self, target: Target, *, limit: int = 100) -> list[CellHit]:
        if target.live:
            return self.live.errors(target.handle, limit=limit)
        return self.closed.errors(Path(target.path), limit=limit)

    def open_workbooks(self) -> list[tuple[str, str]]:
        return self.live.workbooks()


__all__ = ["CellHit", "ClosedExcel", "ExcelService", "LiveExcel", "NO_WORKBOOK", "RangeData", "SheetInfo", "Target", "WorkbookInfo", "WorkbookUnavailable"]
