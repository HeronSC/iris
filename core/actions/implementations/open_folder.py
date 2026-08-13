from __future__ import annotations

from pathlib import Path

from core.actions.executor import ActionExecutionContext, path_is_allowed
from core.actions.models import ActionRequest, ActionResult, ValidationResult


class OpenFolderAction:
    name = "open_folder"

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        file_id = str(request.arguments.get("file_id", "")).strip()
        if not file_id:
            return ValidationResult(ok=False, error="Missing file_id")
        record = context.catalog.get_by_id(file_id)
        if record is None:
            return ValidationResult(ok=False, error=f"File not found: {file_id}")

        file_path = Path(record.path)
        folder_path = file_path.parent
        if not folder_path.exists() or not folder_path.is_dir():
            return ValidationResult(ok=False, error="Containing folder no longer exists", resolved_target=str(folder_path))
        if not path_is_allowed(file_path, context.allowed_roots):
            return ValidationResult(ok=False, error="File path is outside configured roots", resolved_target=str(file_path.resolve()))

        resolved_folder = str(folder_path.resolve())
        return ValidationResult(
            ok=True,
            resolved_target=resolved_folder,
            resolved_arguments={"folder_path": resolved_folder},
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        folder = str(request.arguments.get("folder_path", "")).strip()
        if not folder:
            return ActionResult(status="failed", message="Missing validated folder path", action=self.name, error="missing_folder_path")
        context.system.open_folder(folder)
        return ActionResult(status="success", message="Folder opened", action=self.name, resolved_target=folder)


class ShowInExplorerAction:
    name = "show_in_explorer"

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        file_id = str(request.arguments.get("file_id", "")).strip()
        if not file_id:
            return ValidationResult(ok=False, error="Missing file_id")
        record = context.catalog.get_by_id(file_id)
        if record is None:
            return ValidationResult(ok=False, error=f"File not found: {file_id}")

        file_path = Path(record.path)
        if not file_path.exists() or not file_path.is_file():
            return ValidationResult(ok=False, error="File no longer exists", resolved_target=str(file_path))
        if not path_is_allowed(file_path, context.allowed_roots):
            return ValidationResult(ok=False, error="File path is outside configured roots", resolved_target=str(file_path.resolve()))

        resolved_file = str(file_path.resolve())
        return ValidationResult(
            ok=True,
            resolved_target=resolved_file,
            resolved_arguments={"path": resolved_file},
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        target = str(request.arguments.get("path", "")).strip()
        if not target:
            return ActionResult(status="failed", message="Missing validated file path", action=self.name, error="missing_path")
        context.system.show_in_explorer(target)
        return ActionResult(status="success", message="Opened Explorer selection", action=self.name, resolved_target=target)

