# File: core/actions/implementations/active_context.py

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.results.models import Result, Source, file, status
from core.results.models import text as text_result
from core.tools.models import PermissionLevel, ToolDefinition


class ActiveContextArguments(BaseModel):
    which: str = Field(default="current", description="current for what the user is looking at now, previous for the window before it, history for the recent list")


class ActiveContextAction:

    name = "active_context"
    definition = ToolDefinition(
        name="active_context",
        description=(
            "What the user is looking at right now: the active application, the open file, workbook, folder or project, and the selection "
            "where the app exposes one. Use it to resolve 'this', 'here', 'the file I have open', 'my workbook', 'this project'."
        ),
        arguments=ActiveContextArguments,
        permission=PermissionLevel.READ,
        keywords=("this file", "this workbook", "this document", "this folder", "this project", "what i have open", "i have open", "looking at", "current window", "active window", "on my screen"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ActiveContextArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        which = arguments.which.strip().lower() or "current"
        if which not in {"current", "previous", "history"}:
            return ValidationResult(ok=False, error="which is one of current, previous, history")
        return ValidationResult(ok=True, resolved_arguments={"which": which})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service = getattr(context, "context_service", None)
        source = Source("active_context", "tool", "foreground window")
        if service is None:
            message = "Context capture is not running in this host, so what is on screen is unknown."
            return ActionResult(status="failed", message=message, action=self.name, error="context_unavailable", results=(status("warning", message, source=source),))
        if getattr(service, "paused", False):
            message = "Context capture is paused (/context resume turns it back on), so what is on screen is unknown."
            return ActionResult(status="failed", message=message, action=self.name, error="context_paused", results=(status("warning", message, source=source),))
        which = str(request.arguments.get("which") or "current")
        if which == "history":
            entries = service.history()
            if not entries:
                return ActionResult(status="success", message="No windows have been seen yet.", action=self.name)
            lines = [f"{index}. {entry.describe()} ({entry.captured_at[11:16]} UTC)" for index, entry in enumerate(entries, start=1)]
            return ActionResult(status="success", message="Recent windows, newest first:\n" + "\n".join(lines), action=self.name)
        picked = service.previous() if which == "previous" else service.current()
        if picked is None:
            message = "No window has been seen yet." if which == "current" else "There is no earlier window in the history."
            return ActionResult(status="success", message=message, action=self.name)
        results: list[Result] = []
        description = ("The user is looking at " if which == "current" else "Before the current window the user had ") + picked.describe() + "."
        if picked.target and _is_path(picked.target):
            path = Path(picked.target)
            size = path.stat().st_size if path.is_file() else None
            results.append(file(str(path), source=Source("active_context", "tool", str(path)), size_bytes=size, title=picked.target_name))
        results.append(text_result(description, source=source, format="text", title=picked.app))
        return ActionResult(status="success", message=description, action=self.name, resolved_target=picked.target, results=tuple(results))


def _is_path(value: str) -> bool:
    return len(value) > 2 and (value[1] == ":" or value.startswith("\\\\"))


__all__ = ["ActiveContextAction", "ActiveContextArguments"]
