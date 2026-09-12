# File: core/actions/implementations/excel_tools.py

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.excel import WorkbookUnavailable
from core.excel.models import column_letter
from core.results.models import Result, Source, status, table
from core.results.models import text as text_result
from core.tools.models import PermissionLevel, ToolDefinition

NO_SERVICE = "Excel access is not running in this host."


def _service(context: object) -> Any | None:
    return getattr(context, "excel_service", None)


def _target(context: object, path: str | None) -> tuple[Any, Any | None, str | None]:
    service = _service(context)
    if service is None:
        return None, None, NO_SERVICE
    try:
        return service, service.resolve(path), None
    except WorkbookUnavailable as error:
        return service, None, str(error)


class WorkbookArguments(BaseModel):
    path: str | None = Field(default=None, description="Workbook file path or the name of an open workbook; the workbook in view when omitted")


class ExcelWorkbookAction:

    name = "excel_workbook"
    definition = ToolDefinition(
        name="excel_workbook",
        description="Describe an Excel workbook: whether it is open in Excel or read from disk, its sheets with sizes, tables, pivot tables, named ranges, the active sheet and selection, and unsaved changes.",
        arguments=WorkbookArguments,
        permission=PermissionLevel.READ,
        keywords=("workbook", "spreadsheet", "excel", "sheets", "named range", "pivot", "this workbook", "the workbook i have open", "xlsx"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = WorkbookArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        _service_obj, target, problem = _target(context, arguments.path)
        if problem:
            return ValidationResult(ok=False, error=problem)
        return ValidationResult(ok=True, resolved_target=target.path, resolved_arguments={"path": target.path})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service, target, problem = _target(context, request.arguments.get("path"))
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="workbook_unavailable")
        try:
            info = service.describe(target)
        except (WorkbookUnavailable, Exception) as error:
            return ActionResult(status="failed", message=f"Could not read {target.name}: {error}", action=self.name, error="read_failed")
        source = Source("excel_workbook", "document", info.path)
        results: list[Result] = [text_result(info.describe(), source=source, title=info.name, format="text")]
        results.append(table(("sheet", "rows", "columns", "tables", "pivots", "flags"), [(s.name, s.rows, s.columns, ", ".join(s.tables), s.pivots, ", ".join(flag for flag, on in (("hidden", s.hidden), ("protected", s.protected)) if on)) for s in info.sheets], source=source, title="Sheets"))
        if info.names:
            results.append(table(("name", "refers to"), list(info.names.items()), source=source, title="Named ranges"))
        if info.live and info.saved is False:
            results.append(status("warning", "The workbook has unsaved changes; values are what is on screen now.", source=source))
        return ActionResult(status="success", message=info.describe(), action=self.name, resolved_target=info.path, results=tuple(results))


class ReadArguments(BaseModel):
    path: str | None = Field(default=None, description="Workbook path or open workbook name; the workbook in view when omitted")
    sheet: str | None = Field(default=None, description="Sheet name; the active sheet when omitted")
    range: str | None = Field(default=None, description="A1-style range such as A1:D20 or a table or named range; the used range when omitted")
    formulas: bool = Field(default=False, description="Also return the formulas behind the values")
    max_rows: int = Field(default=60, ge=1, le=500)


class ExcelReadAction:

    name = "excel_read"
    definition = ToolDefinition(
        name="excel_read",
        description="Read cell values (and optionally formulas) from a sheet or range of an Excel workbook, open or on disk. Returns a table.",
        arguments=ReadArguments,
        permission=PermissionLevel.READ,
        keywords=("cells", "range", "column", "row", "values in", "formula in", "what is in cell", "read the sheet", "sheet", "a1"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ReadArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        _service_obj, target, problem = _target(context, arguments.path)
        if problem:
            return ValidationResult(ok=False, error=problem)
        resolved = arguments.model_dump()
        resolved["path"] = target.path
        return ValidationResult(ok=True, resolved_target=f"{target.name}!{arguments.sheet or ''}{arguments.range or ''}", resolved_arguments=resolved)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        arguments = request.arguments
        service, target, problem = _target(context, arguments.get("path"))
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="workbook_unavailable")
        try:
            data = service.read(target, arguments.get("sheet") or None, arguments.get("range") or None, formulas=bool(arguments.get("formulas")), max_rows=int(arguments.get("max_rows") or 60))
        except Exception as error:
            return ActionResult(status="failed", message=f"Could not read {target.name}: {error}", action=self.name, error="read_failed")
        source = Source("excel_read", "document", f"{target.path}#{data.sheet}!{data.address}")
        width = data.width
        columns = [column_letter(index) for index in range(1, width + 1)]
        rows = [tuple(list(row) + [None] * (width - len(row))) for row in data.rows]
        heading = f"{target.name}, sheet {data.sheet}, range {data.address}: {len(rows)} row{'s' if len(rows) != 1 else ''}" + (" (more not shown)" if data.truncated else "")
        lines = [heading]
        for index, row in enumerate(rows[:40], start=1):
            lines.append(" | ".join("" if value is None else str(value) for value in row))
        results: list[Result] = [table(columns or ("A",), rows or [(None,)], source=source, title=f"{data.sheet}!{data.address}")]
        if data.formulas:
            formula_rows = [tuple(list(row) + [""] * (width - len(row))) for row in data.formulas]
            results.append(table(columns or ("A",), formula_rows, source=source, title=f"Formulas in {data.sheet}!{data.address}"))
            lines.append("Formulas: " + "; ".join(cell for row in data.formulas[:20] for cell in row if str(cell).startswith("=")) [:1500])
        if data.truncated:
            results.append(status("warning", f"Only the first {len(rows)} rows are shown; narrow the range.", source=source))
        return ActionResult(status="success", message="\n".join(lines), action=self.name, resolved_target=f"{target.name}!{data.sheet}!{data.address}", results=tuple(results))


class FindArguments(BaseModel):
    text: str = Field(description="Text or number to look for in cell values")
    path: str | None = Field(default=None)
    sheet: str | None = Field(default=None, description="Only this sheet")
    limit: int = Field(default=50, ge=1, le=500)


class ExcelFindAction:

    name = "excel_find"
    definition = ToolDefinition(
        name="excel_find",
        description="Find cells whose value contains some text in an Excel workbook, open or on disk. Returns sheet, cell and value.",
        arguments=FindArguments,
        permission=PermissionLevel.READ,
        keywords=("which cell", "find in the workbook", "where in the sheet", "search the workbook", "look for in excel", "cells containing"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = FindArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if not arguments.text.strip():
            return ValidationResult(ok=False, error="Say what to look for")
        _service_obj, target, problem = _target(context, arguments.path)
        if problem:
            return ValidationResult(ok=False, error=problem)
        resolved = arguments.model_dump()
        resolved["path"] = target.path
        return ValidationResult(ok=True, resolved_target=target.path, resolved_arguments=resolved)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        arguments = request.arguments
        service, target, problem = _target(context, arguments.get("path"))
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="workbook_unavailable")
        text = str(arguments.get("text") or "")
        try:
            hits = service.find(target, text, arguments.get("sheet") or None, limit=int(arguments.get("limit") or 50))
        except Exception as error:
            return ActionResult(status="failed", message=f"Could not search {target.name}: {error}", action=self.name, error="read_failed")
        source = Source("excel_find", "document", target.path)
        if not hits:
            message = f"No cell in {target.name} contains {text!r}."
            return ActionResult(status="success", message=message, action=self.name, results=(status("ok", message, source=source),))
        lines = [f"{len(hits)} cell{'s' if len(hits) != 1 else ''} in {target.name} contain {text!r}:"] + [f"{hit.sheet}!{hit.address}: {hit.value}" for hit in hits[:40]]
        return ActionResult(status="success", message="\n".join(lines), action=self.name, resolved_target=target.path, results=(table(("sheet", "cell", "value"), [(hit.sheet, hit.address, hit.value) for hit in hits], source=source, title=f"Cells containing {text}"),))


class ExcelCheckAction:

    name = "excel_check"
    definition = ToolDefinition(
        name="excel_check",
        description="Check an Excel workbook for error values (#REF!, #DIV/0!, #N/A and the rest) and report each cell with its formula.",
        arguments=WorkbookArguments,
        permission=PermissionLevel.READ,
        keywords=("errors in", "#ref", "#div/0", "#n/a", "#value", "broken formulas", "check the workbook", "anything wrong with the sheet"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = WorkbookArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        _service_obj, target, problem = _target(context, arguments.path)
        if problem:
            return ValidationResult(ok=False, error=problem)
        return ValidationResult(ok=True, resolved_target=target.path, resolved_arguments={"path": target.path})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service, target, problem = _target(context, request.arguments.get("path"))
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="workbook_unavailable")
        try:
            hits = service.errors(target)
        except Exception as error:
            return ActionResult(status="failed", message=f"Could not check {target.name}: {error}", action=self.name, error="read_failed")
        source = Source("excel_check", "document", target.path)
        if not hits:
            message = f"No error values in {target.name}."
            return ActionResult(status="success", message=message, action=self.name, resolved_target=target.path, results=(status("ok", message, source=source),))
        lines = [f"{len(hits)} error value{'s' if len(hits) != 1 else ''} in {target.name}:"] + [f"{hit.sheet}!{hit.address}: {hit.value}" + (f" from {hit.formula}" if hit.formula else "") for hit in hits[:40]]
        results: list[Result] = [
            status("warning", f"{len(hits)} cell{'s' if len(hits) != 1 else ''} show an error value", source=source),
            table(("sheet", "cell", "error", "formula"), [(hit.sheet, hit.address, hit.value, hit.formula or "") for hit in hits], source=source, title=f"Errors in {target.name}"),
        ]
        return ActionResult(status="success", message="\n".join(lines), action=self.name, resolved_target=target.path, results=tuple(results))


EXCEL_ACTIONS = (ExcelWorkbookAction, ExcelReadAction, ExcelFindAction, ExcelCheckAction)

__all__ = ["EXCEL_ACTIONS", "ExcelCheckAction", "ExcelFindAction", "ExcelReadAction", "ExcelWorkbookAction"]
