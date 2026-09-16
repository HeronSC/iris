# File: core/assistant/topic_commands.py

from __future__ import annotations

import re
from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.conversation.persistent_memory import TopicMemoryService
from core.results.html import run_url

_MERGE = re.compile(r"^/topic\s+merge\s+(.+?)\s+into\s+(.+)$", re.IGNORECASE)
_MAX_MESSAGE_CHARS = 600


def _md_escape(text: str) -> str:
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", str(text or ""))


def _link(label: str, command: str) -> str:
    return f"[{_md_escape(label)}]({run_url(command)})"


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

        if lowered in {"/topics", "/topics list"}:
            return self._list_topics(state)

        if lowered == "/topics retitle":
            changes = self.memory_service.retitle_topics()
            if not changes:
                emit_output(self.output, "No topic names changed.")
            else:
                emit_output(self.output, f"Renamed {len(changes)} topic(s):")
                for before, after in changes:
                    emit_output(self.output, f"- {before} -> {after}")
            return self._list_topics(state)

        if lowered.startswith("/topic rename "):
            remainder = text[len("/topic rename "):].strip()
            parts = remainder.split(maxsplit=1)
            if len(parts) < 2:
                emit_output(self.output, "Usage: /topic rename <name-or-id> <new name>")
                return True
            success, message = self.memory_service.rename_topic(parts[0], parts[1])
            emit_output(self.output, message)
            if success:
                return self._list_topics(state)
            return True

        if lowered == "/topics prune":
            pruned = self.memory_service.prune_transient_topics()
            if not pruned:
                emit_output(self.output, "Nothing to prune: no greeting, weather, time, or help topics are active.")
            else:
                emit_output(self.output, f"Archived {len(pruned)} topic(s): " + ", ".join(f"{topic.name} ({topic.id})" for topic in pruned))
            return self._list_topics(state)

        merge = _MERGE.match(text)
        if merge is not None:
            success, message = self.memory_service.merge_topics(merge.group(1).strip(), merge.group(2).strip())
            emit_output(self.output, message)
            if success:
                return self._list_topics(state)
            return True

        if lowered.startswith("/topic delete ") or lowered.startswith("/topic archive "):
            needle = text.split(maxsplit=2)[2].strip() if len(text.split(maxsplit=2)) == 3 else ""
            if not needle:
                emit_output(self.output, "Usage: /topic delete <name-or-id>")
                return True
            success, message = self.memory_service.archive_topic(needle)
            emit_output(self.output, message)
            if success:
                return self._list_topics(state)
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
            state["command_detail"] = {"type": "markdown", "title": topic.name, "content": self._topic_markdown(topic)}
            return True

        return False

    def _list_topics(self, state: dict[str, Any]) -> bool:
        topics = self.memory_service.list_topics(limit=50)
        if not topics:
            emit_output(self.output, "No topics found.")
            return True
        entries = []
        for topic in topics:
            entries.append(f"{topic.id}\\. {_link(topic.name, f'/topic {topic.id}')}")
            emit_output(self.output, f"{topic.id}. {topic.name}")
        footer = "Click a topic to review it. Merge duplicates with `/topic merge <source> into <target>`; remove one with `/topic delete <id>`."
        state["command_detail"] = {"type": "markdown", "title": "Topics", "content": "  \n".join(entries) + "\n\n" + footer}
        return True

    def _topic_markdown(self, topic: Any) -> str:
        recall = self.memory_service.build_public_recall(topic.id, [])
        current = recall.get("current_topic") if isinstance(recall, dict) else {}
        if not isinstance(current, dict):
            current = {}
        lines = [f"Topic {topic.id} · last active {topic.last_active_at[:16].replace('T', ' ')} · {_link('back to all topics', '/topics')} · {_link('delete', f'/topic delete {topic.id}')}", ""]
        goal = str(current.get("goal", "")).strip()
        if goal:
            lines += ["**Goal**", "", _md_escape(goal), ""]
        for key, heading in (("requirements", "Requirements"), ("items", "Active items"), ("decisions", "Decisions"), ("open_questions", "Open questions"), ("notes", "Notes")):
            entries = current.get(key) if isinstance(current.get(key), list) else []
            rendered = [self._entry_text(entry) for entry in entries]
            rendered = [item for item in rendered if item]
            if rendered:
                lines.append(f"**{heading}**")
                lines.append("")
                lines += [f"- {_md_escape(item)}" for item in rendered]
                lines.append("")
        if topic.summary.strip() and not goal:
            lines += ["**Summary**", "", _md_escape(topic.summary.strip()), ""]
        messages = self.memory_service.topic_messages(topic.id, limit=40)
        if messages:
            lines += ["**Conversation**", ""]
            for message in messages:
                role = str(message.get("role") or "").capitalize() or "Message"
                content = " ".join(str(message.get("content") or "").split())
                if len(content) > _MAX_MESSAGE_CHARS:
                    content = content[:_MAX_MESSAGE_CHARS].rstrip() + " …"
                stamp = str(message.get("created_at") or "")[:16].replace("T", " ")
                lines.append(f"- **{role}** ({stamp}): {_md_escape(content)}")
        return "\n".join(lines).rstrip()

    def _entry_text(self, entry: Any) -> str:
        if isinstance(entry, dict):
            for key in ("label", "text", "name", "value"):
                value = str(entry.get(key, "")).strip()
                if value:
                    status = str(entry.get("status", "")).strip()
                    return f"{value} ({status})" if status and status.lower() not in {"open", "active"} else value
            return ""
        return str(entry).strip()


__all__ = ["TopicCommandHandler"]
