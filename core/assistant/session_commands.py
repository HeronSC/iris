from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.assistant.protocols import SessionManagerProtocol, SessionRepositoryProtocol
from core.assistant.workflows import SessionCloseWorkflow
from core.conversation.session_summarizer import SessionSummarizer


class SessionCommandHandler:
    def __init__(
        self,
        session_manager: SessionManagerProtocol,
        session_repository: SessionRepositoryProtocol,
        close_workflow: SessionCloseWorkflow | None = None,
        output: OutputSink | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.repository = session_repository
        self.summarizer = SessionSummarizer()
        self.close_workflow = close_workflow
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        if not user_input.startswith("/session"):
            return False

        parts = user_input.strip().split()
        if len(parts) == 1:
            emit_output(self.output, "Usage: /session new|list|resume <id>|current|title <title>|summarize|close")
            return True

        command = parts[1].lower()
        if command == "new":
            active_session = self.session_manager.get_active_session()
            if active_session is not None:
                self.session_manager.close_active_session()
            title = " ".join(parts[2:]).strip() if len(parts) > 2 else None
            session = self.session_manager.start_session(title=title, project_id=state.get("active_project_id"))
            emit_output(self.output, f"Started session: {session.id}")
            return True

        if command == "list":
            sessions = self.repository.list_sessions(limit=10)
            if not sessions:
                emit_output(self.output, "No sessions found.")
            else:
                for item in sessions:
                    emit_output(self.output, f"- {item['id']} | {item['title']} | {item.get('status', 'active')}")
            return True

        if command == "resume":
            if len(parts) < 3:
                emit_output(self.output, "Usage: /session resume <session-id>")
                return True
            session = self.session_manager.resume_session(parts[2])
            state["active_project_id"] = session.project_id
            emit_output(self.output, f"Resumed session: {session.id}")
            return True

        if command == "current":
            session = self.session_manager.get_active_session()
            if session is None:
                emit_output(self.output, "No active session.")
            else:
                emit_output(self.output, f"Active session: {session.id} | {session.title}")
            return True

        if command == "title":
            if len(parts) < 3:
                emit_output(self.output, "Usage: /session title <new title>")
                return True
            session = self.session_manager.get_active_session()
            if session is None:
                emit_output(self.output, "No active session.")
            else:
                session.title = " ".join(parts[2:]).strip()
                self.repository.save_session(session)
                emit_output(self.output, f"Updated title: {session.title}")
            return True

        if command == "summarize":
            session = self.session_manager.get_active_session()
            if session is None:
                emit_output(self.output, "No active session.")
            else:
                session.summary = self.summarizer.summarize(session.summary, session.get_messages())
                session.metadata["last_summarized_message_count"] = len(session.get_messages())
                self.repository.save_session(session)
                emit_output(self.output, "Summary updated.")
            return True

        if command == "close":
            session = self.session_manager.get_active_session()
            if session is None:
                emit_output(self.output, "No active session.")
                return True

            session.summary = self.summarizer.summarize(session.summary, session.get_messages())
            session.metadata["last_summarized_message_count"] = len(session.get_messages())
            self.repository.save_session(session)

            if self.close_workflow is None:
                self.session_manager.close_active_session()
                emit_output(self.output, f"Closed session {session.id}.")
                return True
            generated_count, warning = self.close_workflow.close_active_session_with_scan(session)
            emit_output(self.output, f"Closed session {session.id}. Generated {generated_count} proposal(s).")
            if warning:
                emit_output(self.output, warning)
            return True

        emit_output(self.output, "Unknown /session command.")
        return True

