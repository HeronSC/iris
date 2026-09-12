# File: core/actions/implementations/project_tools.py

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from core.actions.models import (
    ActionRequest,
    ActionResult,
    ConfirmationPreview,
    ValidationResult,
)
from core.results.models import Source, status, table
from core.results.models import text as text_result
from core.tools.models import PermissionLevel, ToolDefinition

NO_SERVICE = "Projects are not available in this host."


def _service(context: object) -> Any | None:
    return getattr(context, "project_service", None)


def _active(service: Any, context: object) -> dict[str, Any] | None:
    active_id = getattr(context, "active_project_id", None)
    if callable(active_id):
        active_id = active_id()
    project = service.get(str(active_id or ""))
    if project is not None:
        return project
    inferred, _reason = service.infer()
    return inferred


class ActiveProjectArguments(BaseModel):
    name: str | None = Field(
        default=None,
        description="A project name or id; the active or in-view project when omitted",
    )


class ActiveProjectAction:
    name = "active_project"
    definition = ToolDefinition(
        name="active_project",
        description="The user's current project: its summary, focus, linked folders, decisions already made, and open tasks. Use it before answering questions about 'the project', 'my tasks', or 'what did we decide'.",
        arguments=ActiveProjectArguments,
        permission=PermissionLevel.READ,
        keywords=(
            "project",
            "my tasks",
            "open tasks",
            "what did we decide",
            "current focus",
            "what am i working on",
            "unfinished",
        ),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ActiveProjectArguments.model_validate(request.arguments)
        except ValidationError as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if _service(context) is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        return ValidationResult(ok=True, resolved_arguments={"name": arguments.name})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service = _service(context)
        name = request.arguments.get("name")
        project = service.get(str(name)) if name else _active(service, context)
        source = Source(
            "active_project", "memory", str(service.path) if service.path else None
        )
        if project is None:
            listing = (
                ", ".join(str(item.get("name")) for item in service.active_projects())
                or "none yet"
            )
            message = f"No project is active or in view. Known projects: {listing}."
            return ActionResult(
                status="success",
                message=message,
                action=self.name,
                results=(status("warning", message, source=source),),
            )
        description = service.describe(project)
        results = [
            text_result(
                description,
                source=source,
                title=str(project.get("name")),
                format="text",
            )
        ]
        tasks = service.open_tasks(project)
        if tasks:
            results.append(
                table(
                    ("id", "task", "since"),
                    [
                        (item["id"], item["text"], item.get("created_at"))
                        for item in tasks
                    ],
                    source=source,
                    title=f"Open tasks for {project.get('name')}",
                )
            )
        decisions = service.active_decisions(project)
        if decisions:
            results.append(
                table(
                    ("decision", "made"),
                    [(item["decision"], item.get("made_at")) for item in decisions],
                    source=source,
                    title=f"Decisions for {project.get('name')}",
                )
            )
        return ActionResult(
            status="success",
            message=description,
            action=self.name,
            resolved_target=str(project.get("id")),
            results=tuple(results),
        )


class ProjectUpdateArguments(BaseModel):
    change: Literal["task", "task_done", "decision", "focus"] = Field(
        description="task adds an open task, task_done closes one by id, decision records a decision, focus sets the current focus"
    )
    text: str = Field(
        default="",
        description="The task, decision, or focus text; for task_done the task id",
    )
    project: str | None = Field(
        default=None,
        description="Project name or id; the active or in-view project when omitted",
    )


class ProjectUpdateAction:
    name = "project_update"
    definition = ToolDefinition(
        name="project_update",
        description="Record a task, close a task, record a decision, or set the focus on the user's project, when they say so plainly.",
        arguments=ProjectUpdateArguments,
        permission=PermissionLevel.WRITE,
        keywords=(
            "add a task",
            "remember to",
            "todo",
            "we decided",
            "decision",
            "mark done",
            "task done",
            "set the focus",
            "focus on",
        ),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ProjectUpdateArguments.model_validate(request.arguments)
        except ValidationError as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        service = _service(context)
        if service is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        project = (
            service.get(str(arguments.project))
            if arguments.project
            else _active(service, context)
        )
        if project is None:
            return ValidationResult(
                ok=False, error="No project is active or in view; say which project."
            )
        text = " ".join(arguments.text.split())
        if arguments.change == "task_done":
            if not text.isdigit():
                return ValidationResult(
                    ok=False, error="task_done needs the task id in text"
                )
        elif not text:
            return ValidationResult(ok=False, error=f"{arguments.change} needs text")
        summary = {
            "task": f"Add task to {project['name']}: {text}",
            "task_done": f"Close task {text} on {project['name']}",
            "decision": f"Record decision for {project['name']}: {text}",
            "focus": f"Set focus of {project['name']}: {text}",
        }[arguments.change]
        return ValidationResult(
            ok=True,
            resolved_target=str(project.get("id")),
            resolved_arguments={
                "change": arguments.change,
                "text": text,
                "project": str(project.get("id")),
            },
            confirmation_preview=ConfirmationPreview(
                summary=summary, target=str(service.path or "projects"), title="Project"
            ),
            changes=(str(service.path),) if service.path else (),
        )

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service = _service(context)
        project = service.get(str(request.arguments.get("project") or ""))
        if project is None:
            return ActionResult(
                status="failed",
                message="The project is gone.",
                action=self.name,
                error="no_project",
            )
        change = str(request.arguments.get("change"))
        text = str(request.arguments.get("text") or "")
        try:
            if change == "task":
                entry = service.add_task(project, text, source="iris")
                message = (
                    f"Task {entry['id']} added to {project['name']}: {entry['text']}"
                )
            elif change == "task_done":
                entry = service.complete_task(project, int(text))
                message = (
                    f"Closed task {entry['id']} on {project['name']}: {entry['text']}"
                )
            elif change == "decision":
                service.add_decision(project, text, source="iris")
                message = f"Recorded for {project['name']}: {text}"
            else:
                service.set_focus(project, text)
                message = f"Focus of {project['name']} is now: {text}"
        except ValueError as error:
            return ActionResult(
                status="failed", message=str(error), action=self.name, error="invalid"
            )
        return ActionResult(
            status="success",
            message=message,
            action=self.name,
            resolved_target=str(project.get("id")),
        )


PROJECT_ACTIONS = (ActiveProjectAction, ProjectUpdateAction)

__all__ = ["PROJECT_ACTIONS", "ActiveProjectAction", "ProjectUpdateAction"]
