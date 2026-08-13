from __future__ import annotations

from core.conversation.session import ConversationSession
from core.conversation.session_repository import SessionRepository


class SessionManager:
    def __init__(self, repository: SessionRepository) -> None:
        self.repository = repository
        self._active_session: ConversationSession | None = None

    def start_session(self, title: str | None = None, project_id: str | None = None) -> ConversationSession:
        session = self.repository.create_session(title=title, project_id=project_id)
        self._active_session = session
        return session

    def resume_session(self, session_id: str) -> ConversationSession:
        session = self.repository.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        session.status = "active"
        self.repository.save_session(session)
        self._active_session = session
        return session

    def add_message(self, role: str, content: str, metadata: dict[str, object] | None = None) -> None:
        if self._active_session is None:
            raise ValueError("No active session")
        self._active_session.add_message(role, content, metadata=metadata)
        self.repository.save_session(self._active_session)

    def set_project(self, project_id: str | None) -> None:
        if self._active_session is None:
            raise ValueError("No active session")
        self._active_session.project_id = project_id
        self.repository.save_session(self._active_session)

    def close_active_session(self) -> None:
        if self._active_session is None:
            raise ValueError("No active session")
        self._active_session.status = "closed"
        self.repository.save_session(self._active_session)
        self._active_session = None

    def get_active_session(self) -> ConversationSession | None:
        return self._active_session

    def save_active_session(self) -> None:
        if self._active_session is None:
            raise ValueError("No active session")
        self.repository.save_session(self._active_session)

    def get_most_recent_session_id(self) -> str | None:
        sessions = self.repository.list_sessions(limit=1)
        if not sessions:
            return None
        session_id = sessions[0].get("id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
        return None

