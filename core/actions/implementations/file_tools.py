# File: core/actions/implementations/file_tools.py

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from core.actions.diffs import unified_text_diff
from core.actions.executor import path_is_allowed
from core.actions.models import (
    ActionRequest,
    ActionResult,
    ConfirmationPreview,
    ValidationResult,
)
from core.results.models import Result, Source, code, status
from core.results.models import diff as diff_result
from core.tools.models import PermissionLevel, ToolDefinition

MAX_BYTES = 2_000_000
MAX_LINES = 400
FORBIDDEN_PARTS = {".git", ".alpackages", "node_modules", "__pycache__"}
LANGUAGES = {".al": "al", ".py": "python", ".json": "json", ".md": "markdown", ".ps1": "powershell", ".js": "javascript", ".ts": "typescript", ".yml": "yaml", ".yaml": "yaml", ".xml": "xml", ".cs": "csharp", ".sql": "sql", ".txt": ""}


class FileRefused(ValueError):
    pass


def _resolve(context: object, raw: str, *, must_exist: bool) -> Path:
    text = (raw or "").strip().strip('"')
    if not text:
        raise FileRefused("Say which file")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise FileRefused(f"{text} is not a full path")
    roots = list(getattr(context, "allowed_roots", None) or [])
    if roots and not path_is_allowed(path, roots):
        raise FileRefused(f"{path} is outside the folders Iris may work in")
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        raise FileRefused(f"{path.name} sits in a folder Iris does not edit ({', '.join(sorted(FORBIDDEN_PARTS))})")
    if must_exist and not path.is_file():
        raise FileRefused(f"{path} does not exist")
    return path


def _read_text(path: Path) -> tuple[str, str, bool]:
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise FileRefused(f"{path.name} is {size // 1_000_000} MB; Iris edits text files under 2 MB")
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise FileRefused(f"{path.name} is not a text file")
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text, newline, bom


def _write_text(path: Path, text: str, newline: str, bom: bool) -> None:
    normalized = text.replace("\r\n", "\n").replace("\n", newline) if newline != "\n" else text
    payload = normalized.encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + payload)


def _language(path: Path) -> str:
    return LANGUAGES.get(path.suffix.lower(), "")


def _hint(path: Path) -> str:
    return " Run al_compile afterwards and fix what it reports." if path.suffix.lower() == ".al" else ""


class ReadArguments(BaseModel):
    path: str = Field(description="Full path of the file to read")
    start_line: int = Field(default=1, ge=1, description="First line to return")
    max_lines: int = Field(default=200, ge=1, le=MAX_LINES)


class ReadFileAction:

    name = "read_file"
    definition = ToolDefinition(
        name="read_file",
        description="Read a text file inside the folders Iris may work in, with line numbers, a window at a time. Read before editing so an edit targets exact text.",
        arguments=ReadArguments,
        permission=PermissionLevel.READ,
        keywords=("read the file", "open the file", "show me the file", "contents of", "what is in the file", "lines of"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ReadArguments.model_validate(request.arguments)
            path = _resolve(context, arguments.path, must_exist=True)
        except FileRefused as error:
            return ValidationResult(ok=False, error=str(error))
        except ValidationError as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        return ValidationResult(ok=True, resolved_target=str(path), resolved_arguments={"path": str(path), "start_line": arguments.start_line, "max_lines": arguments.max_lines})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        path = Path(str(request.arguments["path"]))
        try:
            text, _newline, _bom = _read_text(path)
        except FileRefused as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="refused")
        except OSError as error:
            return ActionResult(status="failed", message=f"Could not read {path.name}: {error}", action=self.name, error="read_failed")
        lines = text.splitlines()
        start = int(request.arguments.get("start_line") or 1)
        count = int(request.arguments.get("max_lines") or 200)
        window = lines[start - 1 : start - 1 + count]
        end = start + len(window) - 1 if window else start - 1
        numbered = "\n".join(f"{number:>5}  {line}" for number, line in enumerate(window, start=start))
        heading = f"{path} lines {start}-{end} of {len(lines)}" + (" (more follows)" if end < len(lines) else "")
        source = Source("read_file", "document", str(path))
        results: tuple[Result, ...] = (code("\n".join(window), source=source, language=_language(path), path=str(path), title=f"{path.name} {start}-{end}"),)
        return ActionResult(status="success", message=heading + "\n" + numbered, action=self.name, resolved_target=str(path), results=results)


class EditArguments(BaseModel):
    path: str = Field(description="Full path of the file to change")
    old_text: str = Field(description="The exact text to replace, including its indentation; it must occur exactly as often as occurrences says")
    new_text: str = Field(description="The replacement text")
    occurrences: int = Field(default=1, ge=1, le=50, description="How many times old_text is expected in the file; all of them are replaced")


class EditFileAction:

    name = "edit_file"
    definition = ToolDefinition(
        name="edit_file",
        description="Replace exact text in a file inside the folders Iris may work in. Shows the diff for approval, copies the file first so /undo can put it back, keeps the file's line endings. After editing AL, run al_compile and fix what it reports.",
        arguments=EditArguments,
        permission=PermissionLevel.WRITE,
        requires_confirmation=True,
        keywords=("edit the file", "change the code", "replace in the file", "fix the code", "update the procedure", "rename in the file", "modify the file"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = EditArguments.model_validate(request.arguments)
            path = _resolve(context, arguments.path, must_exist=True)
            text, newline, _bom = _read_text(path)
        except FileRefused as error:
            return ValidationResult(ok=False, error=str(error))
        except OSError as error:
            return ValidationResult(ok=False, error=f"Could not read the file: {error}")
        except ValidationError as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if not arguments.old_text:
            return ValidationResult(ok=False, error="old_text is empty; use write_file to create or replace a whole file")
        normalized = text.replace("\r\n", "\n")
        old = arguments.old_text.replace("\r\n", "\n")
        new = arguments.new_text.replace("\r\n", "\n")
        found = normalized.count(old)
        if found == 0:
            return ValidationResult(ok=False, error=f"old_text was not found in {path.name}; read the file and copy the exact text")
        if found != arguments.occurrences:
            return ValidationResult(ok=False, error=f"old_text occurs {found} time{'s' if found != 1 else ''} in {path.name}, not {arguments.occurrences}; make it unique or set occurrences to {found}")
        if old == new:
            return ValidationResult(ok=False, error="old_text and new_text are the same; nothing to change")
        after = normalized.replace(old, new)
        diff = unified_text_diff(normalized, after, label=str(path))
        changed_lines = sum(1 for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
        return ValidationResult(
            ok=True,
            resolved_target=str(path),
            resolved_arguments={"path": str(path), "old_text": old, "new_text": new, "occurrences": arguments.occurrences},
            confirmation_preview=ConfirmationPreview(
                summary=f"Edit {path.name}: replace {found} occurrence{'s' if found != 1 else ''}, {changed_lines} line{'s' if changed_lines != 1 else ''} change",
                target=str(path),
                impact="The file is copied first, so /undo puts it back." + _hint(path),
                title="Edit file",
                metadata={"diff": diff, "diff_path": str(path), "newline": newline},
            ),
            changes=(str(path),),
        )

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        path = Path(str(request.arguments["path"]))
        old = str(request.arguments["old_text"])
        new = str(request.arguments["new_text"])
        try:
            text, newline, bom = _read_text(path)
            normalized = text.replace("\r\n", "\n")
            if normalized.count(old) == 0:
                return ActionResult(status="failed", message=f"{path.name} changed since the preview; old_text is no longer there", action=self.name, error="stale")
            after = normalized.replace(old, new)
            _write_text(path, after, newline, bom)
        except FileRefused as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="refused")
        except OSError as error:
            return ActionResult(status="failed", message=f"Could not write {path.name}: {error}", action=self.name, error="write_failed")
        diff = unified_text_diff(normalized, after, label=str(path))
        source = Source("edit_file", "document", str(path))
        message = f"Edited {path}." + _hint(path)
        return ActionResult(status="success", message=message, action=self.name, resolved_target=str(path), results=(diff_result(diff, source=source, path=str(path), title=path.name),))


class WriteArguments(BaseModel):
    path: str = Field(description="Full path of the file to create or replace")
    content: str = Field(description="The whole content of the file")
    overwrite: bool = Field(default=False, description="Replace the file if it already exists")


class WriteFileAction:

    name = "write_file"
    definition = ToolDefinition(
        name="write_file",
        description="Create a text file, or replace one whole when overwrite is set, inside the folders Iris may work in. Shows the diff for approval and copies an existing file first so /undo can put it back.",
        arguments=WriteArguments,
        permission=PermissionLevel.WRITE,
        requires_confirmation=True,
        keywords=("create a file", "new file", "write a file", "save as", "make a file called", "generate a file"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = WriteArguments.model_validate(request.arguments)
            path = _resolve(context, arguments.path, must_exist=False)
        except FileRefused as error:
            return ValidationResult(ok=False, error=str(error))
        except ValidationError as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        exists = path.is_file()
        if exists and not arguments.overwrite:
            return ValidationResult(ok=False, error=f"{path.name} already exists; use edit_file for a change, or set overwrite to replace it")
        if path.exists() and not path.is_file():
            return ValidationResult(ok=False, error=f"{path} is a folder")
        before = ""
        newline = "\n"
        if exists:
            try:
                before, newline, _bom = _read_text(path)
            except FileRefused as error:
                return ValidationResult(ok=False, error=str(error))
        content = arguments.content.replace("\r\n", "\n")
        diff = unified_text_diff(before.replace("\r\n", "\n"), content, label=str(path))
        line_count = len(content.splitlines())
        summary = f"Replace {path.name} ({line_count} line{'s' if line_count != 1 else ''})" if exists else f"Create {path.name} ({line_count} line{'s' if line_count != 1 else ''})"
        return ValidationResult(
            ok=True,
            resolved_target=str(path),
            resolved_arguments={"path": str(path), "content": content, "overwrite": arguments.overwrite},
            confirmation_preview=ConfirmationPreview(
                summary=summary,
                target=str(path),
                impact=("The file is copied first, so /undo puts it back." if exists else "A new file; /undo removes it again.") + _hint(path),
                title="Write file",
                metadata={"diff": diff, "diff_path": str(path), "newline": newline},
            ),
            changes=(str(path),),
        )

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        path = Path(str(request.arguments["path"]))
        content = str(request.arguments["content"])
        newline = "\n"
        bom = False
        existed = path.is_file()
        try:
            if existed:
                _before, newline, bom = _read_text(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_text(path, content, newline, bom)
        except FileRefused as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="refused")
        except OSError as error:
            return ActionResult(status="failed", message=f"Could not write {path.name}: {error}", action=self.name, error="write_failed")
        source = Source("write_file", "document", str(path))
        message = f"{'Replaced' if existed else 'Created'} {path}." + _hint(path)
        return ActionResult(status="success", message=message, action=self.name, resolved_target=str(path), results=(status("ok", message, source=source),))


FILE_ACTIONS = (ReadFileAction, EditFileAction, WriteFileAction)

__all__ = ["FILE_ACTIONS", "EditFileAction", "FileRefused", "ReadFileAction", "WriteFileAction"]
