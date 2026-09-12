# File: core/excel/__init__.py

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from core.excel.closed import ClosedExcel, WorkbookUnavailable
from core.actions.diffs import unified_text_diff
from core.excel.live import ExcelBusy, ExcelWriteRefused, LiveExcel, LiveWriter, WriteReport, excel_application, normalize_grid
from core.excel.models import CellHit, RangeData, SheetInfo, WorkbookInfo, cell_address

logger = logging.getLogger(__name__)

NO_WORKBOOK = "No workbook is in view. Open one in Excel, or say which file."
EDIT_NEEDS_EXCEL = "Edits go through Excel itself, so open the workbook in Excel first; files on disk are read-only here."
MAX_HISTORY = 20


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
        self.writer = LiveWriter(self.live)
        self.history: list[CellChange] = []

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
        except (OSError, ValueError, RuntimeError, TypeError):
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

    def preview_write(self, target: Target, sheet: str | None, address: str, values: Any) -> dict[str, Any]:
        if not target.live:
            raise WorkbookUnavailable(EDIT_NEEDS_EXCEL)
        grid = normalize_grid(values)
        sheet_name, resolved, formulas, current, rows, columns = self.writer.current(target.handle, sheet, address)
        if len(grid) != rows or len(grid[0]) != columns:
            raise ExcelWriteRefused(f"{resolved} is {rows} x {columns} but {len(grid)} x {len(grid[0])} values were given")
        _target_sheet, protected = self.writer.sheet_state(target.handle, sheet)
        if protected:
            raise ExcelWriteRefused(f"Sheet {sheet_name} is protected; unprotect it in Excel first")
        label = f"{target.name} {sheet_name}!{resolved}"
        diff = unified_text_diff("\n".join(grid_lines(formulas, resolved)) + "\n", "\n".join(grid_lines(grid, resolved)) + "\n", label=label)
        changed = sum(1 for before_row, after_row in zip(formulas, grid) for before, after in zip(before_row, after_row) if str(before if before is not None else "") != str(after if after is not None else ""))
        return {"sheet": sheet_name, "address": resolved, "before": formulas, "values": current, "after": tuple(tuple(row) for row in grid), "diff": diff, "changed": changed, "cells": rows * columns, "label": label}

    def write(self, target: Target, sheet: str | None, address: str, values: Any) -> WriteReport:
        if not target.live:
            raise WorkbookUnavailable(EDIT_NEEDS_EXCEL)
        report = self.writer.write(target.handle, sheet, address, values)
        self.history.append(CellChange(path=target.path, sheet=report.sheet, address=report.address, before=report.before, after=report.after, made_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat()))
        del self.history[:-MAX_HISTORY]
        return report

    def undo_last(self) -> tuple[CellChange, WriteReport]:
        if not self.history:
            raise WorkbookUnavailable("Nothing to put back; no cells were written in this session.")
        change = self.history[-1]
        handle = self.live.workbook(change.path)
        if handle is None:
            raise WorkbookUnavailable(f"{Path(change.path).name} is no longer open in Excel, so its cells cannot be put back from here.")
        report = self.writer.write(handle, change.sheet, change.address, [list(row) for row in change.before], check_errors=False)
        self.history.pop()
        return change, report


@dataclass(frozen=True)
class CellChange:
    path: str
    sheet: str
    address: str
    before: tuple[tuple[Any, ...], ...]
    after: tuple[tuple[Any, ...], ...]
    made_at: str

    @property
    def label(self) -> str:
        return f"{Path(self.path).name} {self.sheet}!{self.address}"


def grid_lines(grid: Sequence[Sequence[Any]], address: str) -> list[str]:
    match = re.match(r"^\$?([A-Za-z]{1,3})\$?(\d+)", address.split("!")[-1])
    start_column = 1
    start_row = 1
    if match:
        start_column = column_index(match.group(1).upper())
        start_row = int(match.group(2))
    lines: list[str] = []
    for row_offset, row in enumerate(grid):
        for column_offset, value in enumerate(row):
            lines.append(f"{cell_address(start_row + row_offset, start_column + column_offset)} = {'' if value is None else value}")
    return lines


def column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index


__all__ = ["CellChange", "CellHit", "ClosedExcel", "EDIT_NEEDS_EXCEL", "ExcelBusy", "ExcelService", "ExcelWriteRefused", "LiveExcel", "NO_WORKBOOK", "RangeData", "SheetInfo", "Target", "WorkbookInfo", "WorkbookUnavailable", "WriteReport", "grid_lines"]
