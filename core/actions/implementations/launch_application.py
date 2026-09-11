# File: core/actions/implementations/launch_application.py

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from core.actions.executor import ActionExecutionContext
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.tools.models import PermissionLevel, ToolDefinition


class LaunchApplicationArguments(BaseModel):
    app_name: str = Field(description="Name or alias of an application configured in Iris, for example 'visual studio code'")
    target: str | None = Field(default=None, description="Optional file or folder path to open in the application")


class LaunchApplicationAction:
    name = "launch_application"
    definition = ToolDefinition(
        name="launch_application",
        description="Launch an installed desktop application that Iris knows by name or alias, optionally opening a file or folder in it.",
        arguments=LaunchApplicationArguments,
        permission=PermissionLevel.EXECUTE,
        keywords=("launch", "open", "start", "run", "app", "program"),
    )

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

        target = str(request.arguments.get("target") or "").strip() or None
        if target is not None:
            target_path = Path(target).expanduser()
            if not target_path.exists():
                return ValidationResult(ok=False, error=f"Target not found: {target}", resolved_target=str(executable))
            target = str(target_path.resolve())

        return ValidationResult(
            ok=True,
            resolved_target=str(executable),
            resolved_arguments={
                "executable": str(executable),
                "display_name": context.applications[app_id].display_name,
                "target": target,
            },
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        executable = str(request.arguments.get("executable", "")).strip()
        if not executable:
            return ActionResult(status="failed", message="Missing validated executable path", action=self.name, error="missing_executable")
        display_name = str(request.arguments.get("display_name", "application")).strip() or "application"
        target = str(request.arguments.get("target") or "").strip() or None
        if target:
            context.system.launch_application(executable, target)
        else:
            context.system.launch_application(executable)
        message = f"Opened {target} in {display_name}" if target else f"Launched {display_name}"
        return ActionResult(
            status="success",
            message=message,
            action=self.name,
            resolved_target=executable,
        )
