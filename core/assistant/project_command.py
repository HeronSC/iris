from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output


class ProjectCommandHandler:
    def __init__(self, output: OutputSink | None = None) -> None:
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        if user_input.strip().lower() == "/project list":
            projects = state["store"].get_projects()
            if not projects:
                emit_output(self.output, "No projects found.")
            else:
                for project in projects:
                    emit_output(self.output, f"- {project.get('id', '')} | {project.get('name', '')} | {project.get('status', '')}")
            return True

        if not user_input.startswith("/project "):
            return False

        project_name = user_input[len("/project "):].strip()
        if not project_name or project_name.lower() == "clear":
            state["active_project_id"] = None
            session_manager = state.get("session_manager")
            if session_manager is not None:
                try:
                    session_manager.set_project(None)
                except ValueError:
                    pass
            emit_output(self.output, "Project cleared.")
        else:
            project = state["store"].get_project(project_name)
            if project is None:
                emit_output(self.output, f"Project not found: {project_name}")
            else:
                state["active_project_id"] = project.get("id")
                session_manager = state.get("session_manager")
                if session_manager is not None:
                    try:
                        session_manager.set_project(state["active_project_id"])
                    except ValueError:
                        pass
                emit_output(self.output, f"Active project: {project['name']}")
        return True
