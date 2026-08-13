from __future__ import annotations

from core.actions.executor import ActionExecutionContext
from core.actions.models import ActionRequest, ActionResult, ValidationResult


class ClipboardAction:
    name = "copy_to_clipboard"

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        text = request.arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return ValidationResult(ok=False, error="Clipboard text is required")
        return ValidationResult(ok=True, resolved_target=text, resolved_arguments={"text": text})

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        text = str(request.arguments.get("text", ""))
        context.system.copy_text(text)
        return ActionResult(status="success", message="Copied to clipboard", action=self.name, resolved_target=text)

