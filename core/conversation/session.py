from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any


class ConversationSession:
    def __init__(self, max_messages: int | None = 8) -> None:
        self.max_messages = max_messages
        self._messages: list[dict[str, Any]] = []
        self._message_counter: int = 0
        self.id: str | None = None
        self.title: str = "New Session"
        self.created_at: str | None = None
        self.updated_at: str | None = None
        self.status: str = "active"
        self.project_id: str | None = None
        self.summary: str = ""
        self.metadata: dict[str, Any] = {}
        self.active_root: str | None = None
        self.active_project: str | None = None
        self.last_selected_file: str | None = None
        self.last_search_results: list[dict[str, Any]] = []
        self.pending_interaction: dict[str, Any] | None = None
        self.last_tool_result: dict[str, Any] | None = None
        self.last_result_set: dict[str, Any] | None = None

    def add_message(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any]
        self._message_counter = self._message_counter + 1
        entry = {
            "id": f"msg-{self._message_counter:06d}",
            "role": role,
            "content": content,
            "created_at": self._utc_now_iso(),
        }
        if metadata is not None:
            entry["metadata"] = metadata
        self._messages.append(entry)
        if self.max_messages is not None and len(self._messages) > self.max_messages:
            self._messages = self._messages[-self.max_messages :]

    def get_messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [item for item in messages if isinstance(item, dict)]
        self._message_counter = self._infer_message_counter(self._messages)

    def _infer_message_counter(self, messages: list[dict[str, Any]]) -> int:
        max_counter = 0
        for item in messages:
            message_id = str(item.get("id", ""))
            match = re.fullmatch(r"msg-(\d+)", message_id)
            if match is not None:
                value = int(match.group(1))
                if value > max_counter:
                    max_counter = value
        if max_counter > 0:
            return max_counter
        return len(messages)

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
