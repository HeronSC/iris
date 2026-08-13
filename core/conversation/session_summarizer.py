from __future__ import annotations

import re
from typing import Any


class SessionSummarizer:
    def summarize(self, existing_summary: str, messages: list[dict[str, Any]]) -> str:
        base = (existing_summary or "").strip()
        if not messages:
            return base or "No conversation yet."

        non_empty = [item for item in messages if str(item.get("content", "")).strip()]
        if not non_empty:
            return base or "No conversation yet."

        topic = self._topic_line(non_empty)
        decisions = self._decision_lines(non_empty)
        current_state = self._current_state_lines(non_empty)
        open_items = self._open_item_lines(non_empty)
        next_actions = self._next_action_lines(non_empty)

        lines: list[str] = []
        lines.append("Topic:")
        lines.append(f"- {topic}")
        lines.append("")
        lines.append("Decisions:")
        lines.extend(decisions)
        lines.append("")
        lines.append("Current state:")
        lines.extend(current_state)
        lines.append("")
        lines.append("Open items:")
        lines.extend(open_items)
        lines.append("")
        lines.append("Next actions:")
        lines.extend(next_actions)
        segment = "\n".join(lines)
        if not base:
            return segment
        return f"{base}\n\nUpdate:\n{segment}"

    def _topic_line(self, messages: list[dict[str, Any]]) -> str:
        for item in messages:
            if str(item.get("role", "")) == "user":
                content = str(item.get("content", "")).strip()
                if content:
                    return self._trim_sentence(content)
        return "Conversation follow-up and planning."

    def _decision_lines(self, messages: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        user_pattern = re.compile(r"\b(decide|decided|agreed|we will|i will|let's|plan to|going to)\b", re.IGNORECASE)
        confirm_pattern = re.compile(r"\b(yes|agreed|sounds good|do it|let's do that|approved)\b", re.IGNORECASE)

        for index, item in enumerate(messages):
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if not content:
                continue

            if role == "user" and user_pattern.search(content):
                lines.append(f"- {self._trim_sentence(content)}")
            elif role == "assistant" and user_pattern.search(content):
                next_user_content = self._next_user_content(messages, index)
                if next_user_content and confirm_pattern.search(next_user_content):
                    lines.append(f"- {self._trim_sentence(content)}")

            if len(lines) >= 3:
                break
        if not lines:
            lines.append("- No explicit decisions were captured yet.")
        return lines

    def _next_user_content(self, messages: list[dict[str, Any]], current_index: int) -> str:
        next_index = current_index + 1
        while next_index < len(messages):
            candidate = messages[next_index]
            if str(candidate.get("role", "")).strip().lower() == "user":
                return str(candidate.get("content", "")).strip()
            next_index = next_index + 1
        return ""

    def _current_state_lines(self, messages: list[dict[str, Any]]) -> list[str]:
        latest = self._trim_sentence(str(messages[-1].get("content", "")).strip())
        count = len(messages)
        return [
            f"- Latest message: {latest}",
            f"- Message count in session: {count}",
        ]

    def _open_item_lines(self, messages: list[dict[str, Any]]) -> list[str]:
        questions = [
            self._trim_sentence(str(item.get("content", "")).strip())
            for item in messages
            if str(item.get("role", "")) == "user" and "?" in str(item.get("content", ""))
        ]
        if not questions:
            return ["- No unresolved questions detected."]
        return [f"- {question}" for question in questions[-2:]]

    def _next_action_lines(self, messages: list[dict[str, Any]]) -> list[str]:
        action_pattern = re.compile(r"\b(next|todo|follow up|implement|add|fix)\b", re.IGNORECASE)
        actions: list[str] = []
        for item in reversed(messages):
            content = str(item.get("content", "")).strip()
            if content and action_pattern.search(content):
                actions.append(f"- {self._trim_sentence(content)}")
            if len(actions) >= 2:
                break
        if not actions:
            return ["- Continue with the current topic using recent context."]
        actions.reverse()
        return actions

    def _trim_sentence(self, text: str, limit: int = 120) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."
