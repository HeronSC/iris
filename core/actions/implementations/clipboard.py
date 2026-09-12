# File: core/actions/implementations/clipboard.py

from __future__ import annotations

from pydantic import BaseModel, Field

from core.actions.executor import ActionExecutionContext
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.tools.models import PermissionLevel, ToolDefinition


class ClipboardArguments(BaseModel):
    text: str = Field(description="Text to place on the clipboard")


class ClipboardAction:
    name = "copy_to_clipboard"
    definition = ToolDefinition(
        name="copy_to_clipboard",
        description="Copy text to the Windows clipboard.",
        arguments=ClipboardArguments,
        permission=PermissionLevel.WRITE,
        irreversible=True,
        expose_to_model=False,
    )

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        text = request.arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return ValidationResult(ok=False, error="Clipboard text is required")
        return ValidationResult(ok=True, resolved_target=text, resolved_arguments={"text": text})

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        text = str(request.arguments.get("text", ""))
        context.system.copy_text(text)
        return ActionResult(status="success", message="Copied to clipboard", action=self.name, resolved_target=text)

