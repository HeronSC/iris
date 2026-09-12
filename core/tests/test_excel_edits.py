# File: core/tests/test_excel_edits.py

"""Controlled Excel edits (4.1): preview, confirm, recalculate, report new errors, put back."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.excel_tools import ExcelUndoAction, ExcelWriteAction
from core.actions.models import ActionRequest
from core.excel import ExcelService, ExcelWriteRefused, WorkbookUnavailable
from core.excel.live import ExcelBusy, LiveExcel, normalize_grid
from core.results.models import ResultKind

DIV0 = -2146826281


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index


def _parse(address: str) -> tuple[int, int, int, int]:
    cells = re.findall(r"([A-Z]+)(\d+)", address.upper())
    (c1, r1) = cells[0]
    (c2, r2) = cells[-1]
    return int(r1), _column_index(c1), int(r2), _column_index(c2)


class _Sheet:
    def __init__(self, name: str, formulas: dict[str, object], *, protected: bool = False, busy: bool = False) -> None:
        self.Name = name
        self.formulas = dict(formulas)
        self.ProtectContents = protected
        self.busy = busy
        self.ListObjects: list = []
        self.Visible = -1

    def PivotTables(self):
        return SimpleNamespace(Count=0)

    def value_of(self, cell: str):
        raw = self.formulas.get(cell)
        if isinstance(raw, str) and raw.startswith("="):
            if "/0" in raw:
                return DIV0
            match = re.match(r"^=([A-Z]+\d+)\*(\d+)$", raw)
            if match:
                base = self.value_of(match.group(1))
                return float(base) * int(match.group(2)) if isinstance(base, (int, float)) else DIV0
            return 1.0
        return raw

    def _bounds(self):
        rows = [int(re.findall(r"\d+", key)[0]) for key in self.formulas]
        cols = [_column_index(re.findall(r"[A-Z]+", key)[0]) for key in self.formulas]
        return (min(rows), min(cols), max(rows), max(cols)) if rows else (1, 1, 1, 1)

    @property
    def UsedRange(self):
        r1, c1, r2, c2 = self._bounds()
        return _Range(self, f"{_letters(c1)}{r1}:{_letters(c2)}{r2}")

    def Range(self, address: str):
        return _Range(self, address)


def _letters(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


class _Range:
    def __init__(self, sheet: _Sheet, address: str) -> None:
        self.sheet = sheet
        self.address = address
        self.r1, self.c1, self.r2, self.c2 = _parse(address)
        self.Row = self.r1
        self.Column = self.c1
        self.Rows = SimpleNamespace(Count=self.r2 - self.r1 + 1)
        self.Columns = SimpleNamespace(Count=self.c2 - self.c1 + 1)

    def Address(self, row_absolute, column_absolute):
        return self.address if ":" in self.address or self.Rows.Count > 1 or self.Columns.Count > 1 else self.address

    def _cells(self):
        return [[f"{_letters(column)}{row}" for column in range(self.c1, self.c2 + 1)] for row in range(self.r1, self.r2 + 1)]

    def _grid(self, reader):
        grid = tuple(tuple(reader(cell) for cell in row) for row in self._cells())
        if len(grid) == 1 and len(grid[0]) == 1:
            return grid[0][0]
        return grid

    @property
    def Value2(self):
        return self._grid(self.sheet.value_of)

    @property
    def Formula(self):
        return self._grid(lambda cell: self.sheet.formulas.get(cell))

    @Formula.setter
    def Formula(self, payload) -> None:
        if self.sheet.busy:
            raise RuntimeError("Call was rejected by callee. (0x80010001)")
        cells = self._cells()
        rows = payload if isinstance(payload, (list, tuple)) and payload and isinstance(payload[0], (list, tuple)) else [[payload]]
        for row_cells, row_values in zip(cells, rows):
            for cell, value in zip(row_cells, row_values):
                self.sheet.formulas[cell] = value


class _Workbook:
    def __init__(self, full_name: str, sheets: list[_Sheet], *, read_only: bool = False) -> None:
        self.FullName = full_name
        self.Name = Path(full_name).name
        self._sheets = sheets
        self.ActiveSheet = sheets[0]
        self.Saved = True
        self.ReadOnly = read_only
        self.Names: list = []
        self.calculations = 0
        self.Application = SimpleNamespace(Calculate=self._calculate)

    def _calculate(self) -> None:
        self.calculations += 1

    @property
    def Worksheets(self):
        class _Collection(list):
            def __call__(inner, name):
                for sheet in inner:
                    if sheet.Name == name:
                        return sheet
                raise KeyError(name)

        return _Collection(self._sheets)


class _App:
    def __init__(self, workbook: _Workbook) -> None:
        self.Workbooks = [workbook]
        self.ActiveWorkbook = workbook
        self.Selection = SimpleNamespace(Address=lambda r, c: "A1")


def _fixture(**sheet_options):
    sheet = _Sheet("Q3", {"A1": "Item", "B1": "Qty", "A2": "Widget", "B2": 2, "C2": "=B2*5"}, **sheet_options)
    workbook = _Workbook("C:\\Books\\budget.xlsx", [sheet])
    return workbook, _App(workbook)


class GridTests(unittest.TestCase):
    def test_values_normalize_to_rows(self) -> None:
        self.assertEqual(normalize_grid(7), [[7]])
        self.assertEqual(normalize_grid(["a", "b"]), [["a", "b"]])
        self.assertEqual(normalize_grid([[1, 2], [3, 4]]), [[1, 2], [3, 4]])
        with self.assertRaises(ExcelWriteRefused):
            normalize_grid([[1, 2], [3]])
        with self.assertRaises(ExcelWriteRefused):
            normalize_grid([])


class WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workbook, self.app = _fixture()
        self.service = ExcelService([], live=LiveExcel(lambda: self.app))
        self.execution = SimpleNamespace(excel_service=self.service)
        self.write = ExcelWriteAction()
        self.undo = ExcelUndoAction()

    def test_preview_shows_a_diff_and_counts_changes(self) -> None:
        validation = self.write.validate(ActionRequest(action="excel_write", arguments={"range": "B2", "values": 3}), self.execution)
        self.assertTrue(validation.ok)
        preview = validation.confirmation_preview
        self.assertEqual(preview.summary, "Write 1 of 1 cell in budget.xlsx Q3!B2")
        self.assertIn("-B2 = 2", preview.metadata["diff"])
        self.assertIn("+B2 = 3", preview.metadata["diff"])
        self.assertIn("excel_undo", preview.impact)
        self.assertEqual(validation.resolved_arguments["values"], [[3]])
        self.assertTrue(self.write.definition.requires_confirmation)

    def test_write_recalculates_reports_no_errors_and_can_be_undone(self) -> None:
        validation = self.write.validate(ActionRequest(action="excel_write", arguments={"range": "B2", "values": 3}), self.execution)
        result = self.write.execute(ActionRequest(action="excel_write", arguments=validation.resolved_arguments), self.execution)
        self.assertEqual(result.status, "success")
        self.assertIn("Wrote 1 cell in budget.xlsx Q3!B2 and recalculated.", result.message)
        self.assertEqual(self.workbook.calculations, 1)
        self.assertEqual(self.workbook.ActiveSheet.formulas["B2"], 3)
        self.assertEqual([item.kind for item in result.results], [ResultKind.DIFF, ResultKind.STATUS])
        self.assertEqual(len(self.service.history), 1)
        undo_validation = self.undo.validate(ActionRequest(action="excel_undo", arguments={}), self.execution)
        self.assertTrue(undo_validation.ok)
        self.assertIn("+B2 = 2", undo_validation.confirmation_preview.metadata["diff"])
        undone = self.undo.execute(ActionRequest(action="excel_undo", arguments={}), self.execution)
        self.assertEqual(undone.status, "success")
        self.assertEqual(self.workbook.ActiveSheet.formulas["B2"], 2)
        self.assertEqual(self.service.history, [])
        self.assertFalse(self.undo.validate(ActionRequest(action="excel_undo", arguments={}), self.execution).ok)

    def test_a_write_that_creates_an_error_says_so(self) -> None:
        validation = self.write.validate(ActionRequest(action="excel_write", arguments={"range": "C2", "values": "=B2/0"}), self.execution)
        self.assertTrue(validation.ok)
        result = self.write.execute(ActionRequest(action="excel_write", arguments=validation.resolved_arguments), self.execution)
        self.assertIn("1 new error value: Q3!C2 #DIV/0!", result.message)
        titles = [item.title for item in result.results]
        self.assertIn("New errors after the write", titles)
        self.assertEqual(result.results[-1].data["state"], "warning")

    def test_ranges_and_grids_must_agree(self) -> None:
        validation = self.write.validate(ActionRequest(action="excel_write", arguments={"range": "A1:B2", "values": [[1, 2]]}), self.execution)
        self.assertFalse(validation.ok)
        self.assertIn("A1:B2 is 2 x 2 but 1 x 2 values were given", validation.error)
        same = self.write.validate(ActionRequest(action="excel_write", arguments={"range": "B2", "values": 2}), self.execution)
        self.assertFalse(same.ok)
        self.assertIn("already holds those values", same.error)
        block = self.write.validate(ActionRequest(action="excel_write", arguments={"range": "A1:B2", "values": [["Item", "Qty"], ["Gizmo", 9]]}), self.execution)
        self.assertTrue(block.ok)
        self.assertEqual(block.confirmation_preview.summary, "Write 2 of 4 cells in budget.xlsx Q3!A1:B2")

    def test_protected_read_only_busy_and_closed_workbooks_are_refused(self) -> None:
        protected_book, protected_app = _fixture(protected=True)
        service = ExcelService([], live=LiveExcel(lambda: protected_app))
        validation = self.write.validate(ActionRequest(action="excel_write", arguments={"range": "B2", "values": 3}), SimpleNamespace(excel_service=service))
        self.assertFalse(validation.ok)
        self.assertIn("protected", validation.error)
        busy_book, busy_app = _fixture(busy=True)
        busy_service = ExcelService([], live=LiveExcel(lambda: busy_app))
        with self.assertRaises(ExcelBusy) as caught:
            busy_service.write(busy_service.resolve(None), None, "B2", 3)
        self.assertIn("Excel is busy", str(caught.exception))
        self.workbook.ReadOnly = True
        with self.assertRaises(ExcelWriteRefused):
            self.service.write(self.service.resolve(None), None, "B2", 3)
        with self.assertRaises(WorkbookUnavailable) as closed:
            self.service.preview_write(SimpleNamespace(live=False, path="C:\\Books\\other.xlsx", name="other.xlsx", handle=None), None, "A1", 1)
        self.assertIn("open the workbook in Excel first", str(closed.exception))


if __name__ == "__main__":
    unittest.main()
