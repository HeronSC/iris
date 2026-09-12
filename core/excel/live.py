# File: core/excel/live.py

from __future__ import annotations

import logging
from typing import Any, Callable

from core.excel.models import MAX_SCAN_CELLS, CellHit, RangeData, SheetInfo, WorkbookInfo, cell_address, error_text

logger = logging.getLogger(__name__)


def excel_application() -> Any | None:
    try:
        #! @allow-local-import
        import pythoncom
        #! @allow-local-import
        import win32com.client
    except ImportError:
        return None
    try:
        pythoncom.CoInitialize()
        return win32com.client.GetActiveObject("Excel.Application")
    except Exception:
        return None


def _grid(value: Any) -> tuple[tuple[Any, ...], ...]:
    if value is None:
        return ((None,),)
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (list, tuple)):
            return tuple(tuple(row) for row in value)
        return (tuple(value),)
    return ((value,),)


def _clean(value: Any) -> Any:
    marker = error_text(value)
    if marker:
        return marker
    if hasattr(value, "year") and hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class LiveExcel:
    def __init__(self, application: Callable[[], Any | None] = excel_application) -> None:
        self.application = application

    def app(self) -> Any | None:
        try:
            return self.application()
        except Exception as error:
            logger.debug("Excel not reachable: %s", error)
            return None

    def workbooks(self) -> list[tuple[str, str]]:
        app = self.app()
        if app is None:
            return []
        found: list[tuple[str, str]] = []
        try:
            for workbook in app.Workbooks:
                found.append((str(workbook.Name), str(workbook.FullName)))
        except Exception as error:
            logger.debug("Listing workbooks failed: %s", error)
        return found

    def workbook(self, reference: str | None) -> Any | None:
        app = self.app()
        if app is None:
            return None
        try:
            if not reference:
                return app.ActiveWorkbook
            wanted = reference.strip().casefold()
            for workbook in app.Workbooks:
                if str(workbook.FullName).casefold() == wanted or str(workbook.Name).casefold() == wanted or str(workbook.FullName).casefold().endswith(wanted):
                    return workbook
        except Exception as error:
            logger.debug("Finding workbook failed: %s", error)
        return None

    def describe(self, workbook: Any) -> WorkbookInfo:
        app = self.app()
        sheets: list[SheetInfo] = []
        for sheet in workbook.Worksheets:
            used = sheet.UsedRange
            tables = tuple(str(table.Name) for table in sheet.ListObjects)
            try:
                pivots = int(sheet.PivotTables().Count)
            except Exception:
                pivots = 0
            sheets.append(
                SheetInfo(
                    name=str(sheet.Name),
                    rows=int(used.Rows.Count) if used.Value2 is not None else 0,
                    columns=int(used.Columns.Count) if used.Value2 is not None else 0,
                    tables=tables,
                    pivots=pivots,
                    hidden=int(getattr(sheet, "Visible", -1)) != -1,
                    protected=bool(getattr(sheet, "ProtectContents", False)),
                )
            )
        names: dict[str, str] = {}
        try:
            for name in workbook.Names:
                names[str(name.Name)] = str(name.RefersTo).lstrip("=")
        except Exception:
            pass
        selection = None
        active_sheet = None
        try:
            active_sheet = str(workbook.ActiveSheet.Name)
            if app is not None and app.ActiveWorkbook is not None and str(app.ActiveWorkbook.FullName) == str(workbook.FullName):
                selection = str(app.Selection.Address(False, False))
        except Exception:
            pass
        notes: list[str] = []
        if bool(getattr(workbook, "ReadOnly", False)):
            notes.append("Excel has it read-only (another user or OneDrive may hold it).")
        return WorkbookInfo(
            path=str(workbook.FullName),
            name=str(workbook.Name),
            live=True,
            sheets=tuple(sheets),
            names=names,
            active_sheet=active_sheet,
            selection=selection,
            saved=bool(getattr(workbook, "Saved", True)),
            read_only=bool(getattr(workbook, "ReadOnly", False)),
            notes=tuple(notes),
        )

    def read(self, workbook: Any, sheet: str | None, address: str | None, *, formulas: bool = False, max_rows: int = 100) -> RangeData:
        target = workbook.Worksheets(sheet) if sheet else workbook.ActiveSheet
        rng = target.Range(address) if address else target.UsedRange
        values = _grid(rng.Value2)
        formula_rows: tuple[tuple[str, ...], ...] = ()
        if formulas:
            formula_rows = tuple(tuple(str(item) if item is not None else "" for item in row) for row in _grid(rng.Formula))
        truncated = len(values) > max_rows
        rows = tuple(tuple(_clean(item) for item in row) for row in values[:max_rows])
        return RangeData(sheet=str(target.Name), address=str(rng.Address(False, False)), rows=rows, formulas=formula_rows[:max_rows], truncated=truncated)

    def find(self, workbook: Any, text: str, sheet: str | None = None, *, limit: int = 50) -> list[CellHit]:
        needle = text.strip().casefold()
        hits: list[CellHit] = []
        scanned = 0
        sheets = [workbook.Worksheets(sheet)] if sheet else list(workbook.Worksheets)
        for target in sheets:
            used = target.UsedRange
            top_row = int(used.Row)
            left_column = int(used.Column)
            for row_offset, row in enumerate(_grid(used.Value2)):
                for column_offset, value in enumerate(row):
                    scanned += 1
                    if scanned > MAX_SCAN_CELLS:
                        return hits
                    if value is None:
                        continue
                    if needle in str(_clean(value)).casefold():
                        hits.append(CellHit(sheet=str(target.Name), address=cell_address(top_row + row_offset, left_column + column_offset), value=_clean(value)))
                        if len(hits) >= limit:
                            return hits
        return hits

    def errors(self, workbook: Any, *, limit: int = 100) -> list[CellHit]:
        hits: list[CellHit] = []
        scanned = 0
        for target in workbook.Worksheets:
            used = target.UsedRange
            top_row = int(used.Row)
            left_column = int(used.Column)
            values = _grid(used.Value2)
            formulas = _grid(used.Formula)
            for row_offset, row in enumerate(values):
                for column_offset, value in enumerate(row):
                    scanned += 1
                    if scanned > MAX_SCAN_CELLS:
                        return hits
                    marker = error_text(value)
                    if marker is None:
                        continue
                    formula = None
                    try:
                        formula = str(formulas[row_offset][column_offset])
                    except IndexError:
                        formula = None
                    hits.append(CellHit(sheet=str(target.Name), address=cell_address(top_row + row_offset, left_column + column_offset), value=marker, formula=formula))
                    if len(hits) >= limit:
                        return hits
        return hits


__all__ = ["LiveExcel", "excel_application"]
