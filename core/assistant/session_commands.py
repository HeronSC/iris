# File: core/assistant/session_commands.py

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
            emit_output(self.output, "Usage: /session new|list|resume <id>|current|title <title>|summarize|close|search <text>|fork [title]")
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

        if command == "search":
            needle = " ".join(parts[2:]).strip()
            if not needle:
                emit_output(self.output, "Usage: /session search <text>")
                return True
            hits = self.search(needle)
            if not hits:
                emit_output(self.output, f"Nothing in past conversations mentions '{needle}'.")
                return True
            lines = [f"{len(hits)} message{'s' if len(hits) != 1 else ''} mention '{needle}', newest first:"]
            for hit in hits[:20]:
                lines.append(f"- {hit['created_at'][:16]}  {hit['session_id']}  {hit['title']}  [{hit['role']}] {hit['snippet']}")
            lines.append("/session resume <id> reopens one.")
            emit_output(self.output, "\n".join(lines))
            return True

        if command == "fork":
            active_session = self.session_manager.get_active_session()
            if active_session is None:
                emit_output(self.output, "No active session to fork.")
                return True
            title = " ".join(parts[2:]).strip() or f"{active_session.title} (fork)"
            original_id = active_session.id
            self.repository.save_session(active_session)
            fork = self.session_manager.start_session(title=title, project_id=active_session.project_id)
            fork.set_messages(active_session.get_messages())
            fork.summary = active_session.summary
            fork.metadata = {**active_session.metadata, "forked_from": original_id}
            self.repository.save_session(fork)
            emit_output(self.output, f"Forked {original_id} into {fork.id} ({title}); the original keeps everything so far, and this one continues from here.")
            return True

        emit_output(self.output, "Unknown /session command.")
        return True

    def search(self, needle: str, *, limit: int = 20) -> list[dict[str, Any]]:
        wanted = needle.strip().casefold()
        if not wanted:
            return []
        hits: list[dict[str, Any]] = []
        for entry in self.repository.list_sessions(limit=500):
            session_id = str(entry.get("id") or "")
            if not session_id:
                continue
            try:
                session = self.repository.get_session(session_id)
            except (OSError, ValueError, RuntimeError, TypeError):
                continue
            if session is None:
                continue
            for message in session.get_messages():
                content = str(message.get("content") or "")
                position = content.casefold().find(wanted)
                if position < 0:
                    continue
                start = max(0, position - 40)
                snippet = content[start : position + len(wanted) + 60].replace("\n", " ")
                hits.append({"session_id": session_id, "title": session.title, "role": str(message.get("role") or ""), "created_at": str(message.get("created_at") or session.updated_at or ""), "snippet": snippet.strip()})
        hits.sort(key=lambda item: item["created_at"], reverse=True)
        return hits[:limit]

