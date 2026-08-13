from __future__ import annotations

from pathlib import Path

from core.actions.executor import ActionExecutionContext, path_is_allowed
from core.actions.models import ActionRequest, ActionResult, ValidationResult


class OpenFileAction:
    name = "open_file"

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        file_id = str(request.arguments.get("file_id", "")).strip()
        if not file_id:
            return ValidationResult(ok=False, error="Missing file_id")

        record = context.catalog.get_by_id(file_id)
        if record is None:
            return ValidationResult(ok=False, error=f"File not found: {file_id}")

        path = Path(record.path)
        if not path.exists():
            return ValidationResult(ok=False, error="File no longer exists", resolved_target=str(path))
        if not path_is_allowed(path, context.allowed_roots):
            return ValidationResult(ok=False, error="File path is outside configured roots", resolved_target=str(path.resolve()))

        resolved_path = str(path.resolve())
        return ValidationResult(
            ok=True,
            resolved_target=resolved_path,
            resolved_arguments={"path": resolved_path},
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        target = str(request.arguments.get("path", "")).strip()
        if not target:
            return ActionResult(status="failed", message="Missing validated file path", action=self.name, error="missing_path")
        context.system.open_file(target)
        return ActionResult(status="success", message="File opened", action=self.name, resolved_target=target)

