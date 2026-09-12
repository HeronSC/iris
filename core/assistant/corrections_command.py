# File: core/assistant/corrections_command.py

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Callable

from core.assistant.output import OutputSink, emit_output
from core.knowledge.models import MemoryKind, MemoryRecord

TOPIC = "iris/corrections"

REPEAT_THRESHOLD = 3

_WORDS = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    return " ".join(_WORDS.findall(text.lower()))


class CorrectionsCommandHandler:

    def __init__(
        self,
        knowledge: Any,
        *,
        last_request_id: Callable[[], str | None],
        last_user_message: Callable[[], str],
        last_answer: Callable[[], str],
        output: OutputSink | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.last_request_id = last_request_id
        self.last_user_message = last_user_message
        self.last_answer = last_answer
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        lowered = text.lower()
        if lowered == "/correct" or lowered.startswith("/correct "):
            reason = text.split(maxsplit=1)[1].strip() if " " in text else ""
            self._correct(reason)
            return True
        if lowered == "/corrections" or lowered.startswith("/corrections "):
            argument = text.split(maxsplit=1)[1].strip() if " " in text else ""
            self._list(int(argument) if argument.isdigit() else 10)
            return True
        return False

    def _correct(self, reason: str) -> None:
        if not reason:
            emit_output(self.output, "Usage: /correct <what was wrong and why> — it is kept with the request it corrects.")
            return
        asked = self.last_user_message() or ""
        answered = self.last_answer() or ""
        record = self.knowledge.records.add(
            MemoryRecord(
                kind=MemoryKind.OBSERVATION,
                topic=TOPIC,
                content=reason,
                source="user:correction",
                source_ref=self.last_request_id() or None,
                data={"asked": asked[:500], "answered": answered[:800], "normalized": normalize(reason)},
            )
        )
        repeats = self._repeats(normalize(reason))
        lines = [f"Kept: {reason} ({record.id[:8]})."]
        if repeats >= REPEAT_THRESHOLD:
            lines.append(
                f"That is the {repeats}th time this has come up. It may be a principle: "
                f"/knowledge observe iris/principles {reason}"
            )
        emit_output(self.output, "\n".join(lines))

    def _repeats(self, normalized: str) -> int:
        if not normalized:
            return 0
        recent = self.knowledge.records.list_by_topic(TOPIC, kind=MemoryKind.OBSERVATION, limit=500)
        return sum(1 for item in recent if str(item.data.get("normalized", "")) == normalized)

    def _list(self, limit: int) -> None:
        recent = self.knowledge.records.list_by_topic(TOPIC, kind=MemoryKind.OBSERVATION, limit=500)
        if not recent:
            emit_output(self.output, "No corrections recorded. /correct <why> keeps one with the request it corrects.")
            return
        counts = Counter(str(item.data.get("normalized", "")) for item in recent)
        lines = [f"Last {min(limit, len(recent))} of {len(recent)} correction(s):"]
        for item in recent[:limit]:
            asked = str(item.data.get("asked", "")).strip()
            repeat = counts[str(item.data.get("normalized", ""))]
            marker = f" (x{repeat})" if repeat > 1 else ""
            lines.append(f"- {item.created_at[:16]}  {item.content}{marker}" + (f"\n  after: {asked[:100]}" if asked else ""))
        repeated = [(text, count) for text, count in counts.most_common() if count >= REPEAT_THRESHOLD]
        if repeated:
            lines.append("Repeated enough to be principles: " + "; ".join(f"{text} (x{count})" for text, count in repeated))
        emit_output(self.output, "\n".join(lines))
