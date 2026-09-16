# File: core/assistant/keep_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.conversation.creations import CreationStore, derive_title, detect_kind


class KeepCommandHandler:
    def __init__(self, creations: CreationStore, session_manager: Any, output: OutputSink | None = None) -> None:
        self.creations = creations
        self.session_manager = session_manager
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        stripped = user_input.strip()
        lowered = stripped.lower()
        if lowered != "/keep" and not lowered.startswith("/keep "):
            return False
        requested_title = stripped[5:].strip()
        session = self.session_manager.get_active_session() if self.session_manager is not None else None
        messages = session.get_messages() if session is not None else []
        last_assistant = ""
        prompt = ""
        for index in range(len(messages) - 1, -1, -1):
            if str(messages[index].get("role")) == "assistant":
                last_assistant = str(messages[index].get("content") or "")
                for earlier in range(index - 1, -1, -1):
                    if str(messages[earlier].get("role")) == "user":
                        prompt = str(messages[earlier].get("content") or "")
                        break
                break
        if not last_assistant.strip():
            emit_output(self.output, "Nothing to keep yet: Iris has not answered in this session.")
            return True
        kind = detect_kind(prompt, last_assistant) or "note"
        title = requested_title or derive_title(prompt, last_assistant, kind)
        creation = self.creations.save(title=title, content=last_assistant, kind=kind, prompt=prompt, session_id=getattr(session, "id", None))
        emit_output(self.output, f"Kept as {creation.path}")
        return True


__all__ = ["KeepCommandHandler"]
