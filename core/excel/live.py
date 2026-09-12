# File: core/excel/live.py

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    except (OSError, ValueError, RuntimeError, TypeError):
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
        except (OSError, ValueError, RuntimeError, TypeError) as error:
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
        except (OSError, ValueError, RuntimeError, TypeError) as error:
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
        except (OSError, ValueError, RuntimeError, TypeError) as error:
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
            except (OSError, ValueError, RuntimeError, TypeError):
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
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
        selection = None
        active_sheet = None
        try:
            active_sheet = str(workbook.ActiveSheet.Name)
            if app is not None and app.ActiveWorkbook is not None and str(app.ActiveWorkbook.FullName) == str(workbook.FullName):
                selection = str(app.Selection.Address(False, False))
        except (OSError, ValueError, RuntimeError, TypeError):
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


class ExcelBusy(RuntimeError):
    pass


class ExcelWriteRefused(ValueError):
    pass


@dataclass(frozen=True)
class WriteReport:
    sheet: str
    address: str
    before: tuple[tuple[Any, ...], ...]
    after: tuple[tuple[Any, ...], ...]
    new_errors: tuple[CellHit, ...]
    calculated: bool

    @property
    def cells(self) -> int:
        return sum(len(row) for row in self.after)


def normalize_grid(values: Any) -> list[list[Any]]:
    if isinstance(values, (list, tuple)):
        if not values:
            raise ExcelWriteRefused("No values were given")
        if all(isinstance(item, (list, tuple)) for item in values):
            grid = [list(item) for item in values]
            width = len(grid[0])
            if any(len(row) != width for row in grid):
                raise ExcelWriteRefused("Every row must have the same number of values")
            return grid
        return [list(values)]
    return [[values]]


def _busy(error: Exception) -> bool:
    text = str(error).lower()
    return "rejected by callee" in text or "0x80010001" in text or "busy" in text


class LiveWriter:
    def __init__(self, reader: "LiveExcel") -> None:
        self.reader = reader

    def sheet_state(self, workbook: Any, sheet: str | None) -> tuple[Any, bool]:
        target = workbook.Worksheets(sheet) if sheet else workbook.ActiveSheet
        return target, bool(getattr(target, "ProtectContents", False))

    def current(self, workbook: Any, sheet: str | None, address: str) -> tuple[str, str, tuple[tuple[Any, ...], ...], tuple[tuple[Any, ...], ...], int, int]:
        target, _protected = self.sheet_state(workbook, sheet)
        rng = target.Range(address)
        rows = int(rng.Rows.Count)
        columns = int(rng.Columns.Count)
        formulas = tuple(tuple(row) for row in _grid(rng.Formula))
        values = tuple(tuple(_clean(item) for item in row) for row in _grid(rng.Value2))
        return str(target.Name), str(rng.Address(False, False)), formulas, values, rows, columns

    def write(self, workbook: Any, sheet: str | None, address: str, values: Any, *, check_errors: bool = True) -> WriteReport:
        grid = normalize_grid(values)
        try:
            target, protected = self.sheet_state(workbook, sheet)
            if protected:
                raise ExcelWriteRefused(f"Sheet {target.Name} is protected; unprotect it in Excel first")
            if bool(getattr(workbook, "ReadOnly", False)):
                raise ExcelWriteRefused(f"{workbook.Name} is open read-only in Excel")
            rng = target.Range(address)
            rows = int(rng.Rows.Count)
            columns = int(rng.Columns.Count)
            if len(grid) != rows or len(grid[0]) != columns:
                raise ExcelWriteRefused(f"{address} is {rows} x {columns} but {len(grid)} x {len(grid[0])} values were given")
            before = tuple(tuple(row) for row in _grid(rng.Formula))
            errors_before = {(hit.sheet, hit.address) for hit in self.reader.errors(workbook)} if check_errors else set()
            rng.Formula = grid[0][0] if rows == 1 and columns == 1 else tuple(tuple(row) for row in grid)
            calculated = False
            application = getattr(workbook, "Application", None)
            if application is not None and hasattr(application, "Calculate"):
                application.Calculate()
                calculated = True
            after = tuple(tuple(_clean(item) for item in row) for row in _grid(rng.Value2))
            new_errors: tuple[CellHit, ...] = ()
            if check_errors:
                new_errors = tuple(hit for hit in self.reader.errors(workbook) if (hit.sheet, hit.address) not in errors_before)
        except ExcelWriteRefused:
            raise
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            if _busy(error):
                raise ExcelBusy("Excel is busy, probably a cell being edited; finish it and try again") from error
            raise
        return WriteReport(sheet=str(target.Name), address=str(rng.Address(False, False)), before=before, after=after, new_errors=new_errors, calculated=calculated)


__all__ = ["ExcelBusy", "ExcelWriteRefused", "LiveExcel", "LiveWriter", "WriteReport", "excel_application", "normalize_grid"]
