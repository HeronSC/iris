from __future__ import annotations

from pathlib import Path

from core.actions.executor import ActionExecutionContext
from core.actions.models import ActionRequest, ActionResult, ValidationResult


class LaunchApplicationAction:
    name = "launch_application"

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        app_id = str(request.arguments.get("app_id", "")).strip().lower()
        if not app_id:
            app_name = str(request.arguments.get("app_name", "")).strip().lower()
            app_id = context.app_alias_map.get(app_name, "")

        if not app_id or app_id not in context.applications:
            return ValidationResult(ok=False, error="Unknown application")

        executable = Path(context.applications[app_id].executable)
        if not executable.exists():
            return ValidationResult(ok=False, error=f"Executable not found: {executable}", resolved_target=str(executable))

        return ValidationResult(
            ok=True,
            resolved_target=str(executable),
            resolved_arguments={
                "executable": str(executable),
                "display_name": context.applications[app_id].display_name,
            },
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        executable = str(request.arguments.get("executable", "")).strip()
        if not executable:
            return ActionResult(status="failed", message="Missing validated executable path", action=self.name, error="missing_executable")
        display_name = str(request.arguments.get("display_name", "application")).strip() or "application"
        context.system.launch_application(executable)
        return ActionResult(
            status="success",
            message=f"Launched {display_name}",
            action=self.name,
            resolved_target=executable,
        )

