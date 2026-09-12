# File: core/excel/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ERROR_CODES: dict[int, str] = {
    -2146826281: "#DIV/0!",
    -2146826246: "#N/A",
    -2146826259: "#NAME?",
    -2146826288: "#NULL!",
    -2146826252: "#NUM!",
    -2146826265: "#REF!",
    -2146826273: "#VALUE!",
}

ERROR_TEXTS = {"#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!", "#SPILL!", "#CALC!", "#GETTING_DATA"}

MAX_SCAN_CELLS = 250_000


@dataclass(frozen=True)
class SheetInfo:
    name: str
    rows: int
    columns: int
    tables: tuple[str, ...] = ()
    pivots: int = 0
    hidden: bool = False
    protected: bool = False

    @property
    def dimension(self) -> str:
        return f"{self.rows} x {self.columns}" if self.rows and self.columns else "empty"


@dataclass(frozen=True)
class WorkbookInfo:
    path: str
    name: str
    live: bool
    sheets: tuple[SheetInfo, ...] = ()
    names: dict[str, str] = field(default_factory=dict)
    active_sheet: str | None = None
    selection: str | None = None
    saved: bool | None = None
    read_only: bool = False
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        where = "open in Excel" if self.live else "read from disk"
        lines = [f"{self.name} ({where}) at {self.path}"]
        if self.live and self.saved is False:
            lines.append("Has unsaved changes.")
        if self.read_only:
            lines.append("Opened read-only.")
        if self.active_sheet:
            lines.append(f"Active sheet: {self.active_sheet}" + (f", selection {self.selection}" if self.selection else ""))
        lines.append(f"{len(self.sheets)} sheet{'s' if len(self.sheets) != 1 else ''}: " + ", ".join(
            f"{sheet.name} ({sheet.dimension}" + (f", tables {', '.join(sheet.tables)}" if sheet.tables else "") + (f", {sheet.pivots} pivot" + ("s" if sheet.pivots != 1 else "") if sheet.pivots else "") + (", hidden" if sheet.hidden else "") + (", protected" if sheet.protected else "") + ")"
            for sheet in self.sheets
        ))
        if self.names:
            lines.append("Named ranges: " + ", ".join(f"{name} = {ref}" for name, ref in list(self.names.items())[:20]) + (" …" if len(self.names) > 20 else ""))
        lines.extend(self.notes)
        return "\n".join(lines)


@dataclass(frozen=True)
class RangeData:
    sheet: str
    address: str
    rows: tuple[tuple[Any, ...], ...]
    formulas: tuple[tuple[str, ...], ...] = ()
    truncated: bool = False

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)


@dataclass(frozen=True)
class CellHit:
    sheet: str
    address: str
    value: Any
    formula: str | None = None


def error_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in ERROR_CODES:
        return ERROR_CODES[value]
    if isinstance(value, str) and value.strip().upper() in ERROR_TEXTS:
        return value.strip().upper()
    return None


def column_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def cell_address(row: int, column: int) -> str:
    return f"{column_letter(column)}{row}"


__all__ = ["CellHit", "ERROR_CODES", "ERROR_TEXTS", "MAX_SCAN_CELLS", "RangeData", "SheetInfo", "WorkbookInfo", "cell_address", "column_letter", "error_text"]
