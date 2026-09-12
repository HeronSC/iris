# File: core/assistant/learning.py

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from core.knowledge.models import MemoryKind, MemoryRecord
from core.knowledge.principles import GAP_TOPIC, normalize

logger = logging.getLogger(__name__)

CORRECTIONS_TOPIC = "iris/corrections"
EDIT_TOOLS = {"edit_file", "write_file", "excel_write"}
COMPILE_TOOLS = {"al_compile", "compile_workspace"}
EDIT_WINDOW_SECONDS = 900.0


@dataclass(frozen=True)
class LastEdit:
    tool: str
    target: str
    at: float


class LearningLoop:
    def __init__(self, knowledge: Any, *, request_id: Any = None) -> None:
        self.knowledge = knowledge
        self.request_id = request_id
        self.last_edit: LastEdit | None = None

    def _request_id(self) -> str | None:
        value = self.request_id() if callable(self.request_id) else self.request_id
        return str(value) if value else None

    def _record(self, topic: str, content: str, *, source: str, data: dict[str, Any] | None = None) -> MemoryRecord | None:
        try:
            return self.knowledge.records.add(MemoryRecord(kind=MemoryKind.OBSERVATION, topic=topic, content=content, source=source, source_ref=self._request_id(), data={"normalized": normalize(content), **(data or {})}))
        except Exception as error:
            logger.warning("Could not record %s: %s", topic, error)
            return None

    def note_undo(self, report: Any) -> MemoryRecord | None:
        action = str(getattr(report, "action", None) or getattr(getattr(report, "change", None), "action", None) or "a change")
        files = getattr(report, "files", None) or getattr(getattr(report, "change", None), "files", None) or ()
        names = ", ".join(str(item).replace("\\", "/").rsplit("/", 1)[-1] for item in files) or "files"
        content = f"The user undid Iris's change: {action} on {names}"
        return self._record(CORRECTIONS_TOPIC, content, source="user:undo", data={"action": action, "files": [str(item) for item in files]})

    def note_tool_event(self, event: dict[str, Any]) -> MemoryRecord | None:
        if str(event.get("phase")) != "end":
            return None
        name = str(event.get("name") or "")
        now = time.monotonic()
        if name in EDIT_TOOLS and str(event.get("status")) == "success":
            self.last_edit = LastEdit(tool=name, target=str(event.get("target") or ""), at=now)
            return None
        if name in COMPILE_TOOLS and self.last_edit is not None and now - self.last_edit.at <= EDIT_WINDOW_SECONDS:
            summary = str(event.get("summary") or "")
            if "failed to compile" in summary or str(event.get("status")) == "failed":
                edit = self.last_edit
                self.last_edit = None
                return self._record(CORRECTIONS_TOPIC, f"Iris's edit ({edit.tool} on {edit.target or 'a file'}) failed to compile: {summary[:200]}", source="iris:compile", data={"tool": edit.tool, "target": edit.target, "summary": summary[:500]})
            if "compiled:" in summary:
                self.last_edit = None
        return None

    def record_gap(self, question: str, *, context: str = "", source: str = "iris:gap") -> MemoryRecord | None:
        clean = " ".join(question.split())
        if not clean:
            return None
        key = normalize(clean)
        try:
            for existing in self.knowledge.open_observations(GAP_TOPIC):
                if str(existing.data.get("normalized", "")) == key:
                    return existing
        except Exception:
            pass
        return self._record(GAP_TOPIC, clean, source=source, data={"context": context[:500]})


__all__ = ["COMPILE_TOOLS", "CORRECTIONS_TOPIC", "EDIT_TOOLS", "LastEdit", "LearningLoop"]
