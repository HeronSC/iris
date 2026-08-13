from __future__ import annotations

import json
from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.conversation.persistent_memory import TopicMemoryService


class TopicCommandHandler:
    def __init__(self, memory_service: TopicMemoryService, output: OutputSink | None = None) -> None:
        self.memory_service = memory_service
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        text = (user_input or "").strip()
        lowered = text.lower()

        if lowered == "/topic undo":
            session_manager = state.get("session_manager")
            active_session = session_manager.get_active_session() if session_manager is not None and hasattr(session_manager, "get_active_session") else None
            conversation_id = getattr(active_session, "id", None)
            if not conversation_id:
                emit_output(self.output, "No active conversation found.")
                return True
            success, message = self.memory_service.undo_current_topic(str(conversation_id))
            emit_output(self.output, message)
            return True

        if lowered.startswith("/topic undo "):
            needle = text.split(maxsplit=2)[2].strip() if len(text.split(maxsplit=2)) == 3 else ""
            if not needle:
                emit_output(self.output, "Usage: /topic undo <name-or-id>")
                return True
            success, message = self.memory_service.undo_topic(needle)
            emit_output(self.output, message)
            return True

        if lowered == "/topics":
            topics = self.memory_service.list_topics(limit=20)
            if not topics:
                emit_output(self.output, "No topics found.")
                return True
            for topic in topics:
                emit_output(self.output, f"- {topic.id} | {topic.name} | {topic.last_active_at}")
            return True

        if lowered.startswith("/topic"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                emit_output(self.output, "Usage: /topic <name-or-id>")
                return True
            needle = parts[1].strip()
            topic = self.memory_service.get_topic(needle)
            if topic is None:
                emit_output(self.output, "Topic not found.")
                return True
            emit_output(self.output, f"Topic: {topic.id} | {topic.name}")
            recall = self.memory_service.build_public_recall(topic.id, [])
            current = recall.get("current_topic") if isinstance(recall, dict) else None
            if isinstance(current, dict):
                emit_output(self.output, json.dumps(current, indent=2, ensure_ascii=True))
            elif topic.summary.strip():
                emit_output(self.output, topic.summary.strip())
            else:
                emit_output(self.output, "No canonical state available yet.")
            return True

        return False
