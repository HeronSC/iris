# File: core/assistant/project_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.projects.service import PATH_KINDS, ProjectService

USAGE = (
    "Usage: /project | /project list | /project <name> | /project clear | /project use | /project new <name> | "
    "/project link workspace|repository|documents <path> | /project focus <text> | /project decide <text> | "
    "/project tasks | /project task add <text> | /project task done <n>"
)


class ProjectCommandHandler:
    def __init__(self, output: OutputSink | None = None, service: ProjectService | None = None) -> None:
        self.output = output
        self.service = service

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        stripped = user_input.strip()
        if not stripped.lower().startswith("/project"):
            return False
        rest = stripped[len("/project"):].strip()
        service = self.service or self._service_from_state(state)
        if service is None:
            emit_output(self.output, "Projects are not available.")
            return True
        words = rest.split()
        head = words[0].lower() if words else ""
        if not words:
            self._show(service, state)
            return True
        if head == "list":
            projects = service.projects()
            if not projects:
                emit_output(self.output, "No projects found. /project new <name> starts one.")
                return True
            active_id = state.get("active_project_id")
            for project in projects:
                marker = "*" if project.get("id") == active_id else "-"
                emit_output(self.output, f"{marker} {project.get('id', '')} | {project.get('name', '')} | {project.get('status', '')}")
            return True
        if head == "clear":
            self._activate(state, None)
            emit_output(self.output, "Project cleared.")
            return True
        if head == "use":
            project, reason = service.infer()
            if project is None:
                emit_output(self.output, f"No project to adopt: {reason}.")
                return True
            self._activate(state, project.get("id"))
            service.touch(project)
            emit_output(self.output, f"Active project: {project['name']} ({reason}).")
            return True
        if head == "new":
            name = rest[len(words[0]):].strip()
            try:
                project = service.create(name)
            except ValueError as error:
                emit_output(self.output, str(error))
                return True
            self._activate(state, project["id"])
            emit_output(self.output, f"Created and activated project {project['name']} ({project['id']}).")
            return True
        active = self._active(service, state)
        if head == "link":
            if len(words) < 3 or words[1].lower() not in PATH_KINDS:
                emit_output(self.output, USAGE)
                return True
            if active is None:
                emit_output(self.output, "No active project. /project <name> first.")
                return True
            kind = words[1].lower()
            path = rest[len(words[0]) + 1 + len(words[1]):].strip().strip('"')
            try:
                service.link(active, kind, path)
            except ValueError as error:
                emit_output(self.output, str(error))
                return True
            emit_output(self.output, f"Linked {kind} of {active['name']} to {path}.")
            return True
        if head in {"focus", "decide"}:
            text = rest[len(words[0]):].strip()
            if active is None:
                emit_output(self.output, "No active project. /project <name> first.")
                return True
            if not text:
                emit_output(self.output, USAGE)
                return True
            if head == "focus":
                service.set_focus(active, text)
                emit_output(self.output, f"Focus of {active['name']}: {text}")
            else:
                service.add_decision(active, text)
                emit_output(self.output, f"Recorded a decision for {active['name']}.")
            return True
        if head == "tasks":
            if active is None:
                emit_output(self.output, "No active project. /project <name> first.")
                return True
            emit_output(self.output, self._tasks_text(service, active))
            return True
        if head == "task":
            if active is None:
                emit_output(self.output, "No active project. /project <name> first.")
                return True
            if len(words) >= 3 and words[1].lower() == "add":
                text = rest[len(words[0]) + 1 + len(words[1]):].strip()
                entry = service.add_task(active, text)
                emit_output(self.output, f"Task {entry['id']} added to {active['name']}: {entry['text']}")
                return True
            if len(words) == 3 and words[1].lower() == "done" and words[2].isdigit():
                try:
                    entry = service.complete_task(active, int(words[2]))
                except ValueError as error:
                    emit_output(self.output, str(error))
                    return True
                emit_output(self.output, f"Done: {entry['text']}")
                return True
            emit_output(self.output, USAGE)
            return True
        project = service.get(rest)
        if project is None:
            emit_output(self.output, f"Project not found: {rest}")
            return True
        self._activate(state, project.get("id"))
        service.touch(project)
        emit_output(self.output, f"Active project: {project['name']}")
        emit_output(self.output, service.describe(project))
        return True

    def _show(self, service: ProjectService, state: dict[str, Any]) -> None:
        active = self._active(service, state)
        if active is not None:
            emit_output(self.output, service.describe(active))
        else:
            emit_output(self.output, "No active project.")
        inferred, reason = service.infer()
        if inferred is not None and (active is None or inferred.get("id") != active.get("id")):
            emit_output(self.output, f"In view: {inferred['name']} ({reason}). /project use adopts it.")

    def _active(self, service: ProjectService, state: dict[str, Any]) -> dict[str, Any] | None:
        return service.get(str(state.get("active_project_id") or ""))

    def _activate(self, state: dict[str, Any], project_id: str | None) -> None:
        state["active_project_id"] = project_id
        session_manager = state.get("session_manager")
        if session_manager is not None:
            try:
                session_manager.set_project(project_id)
            except ValueError:
                pass

    def _tasks_text(self, service: ProjectService, project: dict[str, Any]) -> str:
        tasks = service.open_tasks(project)
        if not tasks:
            return f"No open tasks for {project['name']}."
        return f"Open tasks for {project['name']}:\n" + "\n".join(f"{item['id']}. {item['text']} (since {item.get('created_at')})" for item in tasks)

    def _service_from_state(self, state: dict[str, Any]) -> ProjectService | None:
        store = state.get("store")
        if store is None:
            return None
        self.service = ProjectService(state.get("memory_path"), store=store)
        return self.service


__all__ = ["ProjectCommandHandler", "USAGE"]
