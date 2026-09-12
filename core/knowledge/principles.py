# File: core/knowledge/principles.py

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.repository import KnowledgeRepository

TOPIC = "iris/principles"
GAP_TOPIC = "iris/open-questions"

_WORDS = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    return " ".join(_WORDS.findall(text.lower()))


@dataclass(frozen=True)
class Principle:
    record: MemoryRecord
    number: int

    @property
    def on(self) -> bool:
        return self.record.status is not MemoryStatus.RETIRED

    @property
    def text(self) -> str:
        return self.record.content

    def describe(self) -> str:
        origin = self.record.data.get("origin")
        note = f" (from {origin})" if origin else ""
        return f"{self.number}. [{'on ' if self.on else 'off'}] {self.text}{note}  {self.record.id[:8]}"


class PrincipleService:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    def all(self) -> list[Principle]:
        records = self.repository.list_by_topic(TOPIC, kind=MemoryKind.KNOWLEDGE, include_superseded=True, limit=500)
        live = [record for record in reversed(records) if record.status is not MemoryStatus.SUPERSEDED]
        return [Principle(record=record, number=index) for index, record in enumerate(live, start=1)]

    def active(self) -> list[Principle]:
        return [item for item in self.all() if item.on]

    def active_lines(self) -> list[str]:
        return [item.text for item in self.active()]

    def find(self, reference: str) -> Principle | None:
        wanted = reference.strip().lower()
        if not wanted:
            return None
        items = self.all()
        if wanted.isdigit():
            number = int(wanted)
            return next((item for item in items if item.number == number), None)
        matches = [item for item in items if item.record.id.startswith(wanted)]
        return matches[0] if len(matches) == 1 else None

    def add(self, text: str, *, source: str = "user", origin: str | None = None) -> tuple[Principle, bool]:
        clean = " ".join(text.split())
        if not clean:
            raise KnowledgeError("A principle needs words")
        key = normalize(clean)
        for item in self.all():
            if normalize(item.text) == key:
                if not item.on:
                    self.switch(item, on=True)
                    return self.find(item.record.id[:8]) or item, False
                return item, False
        data: dict[str, Any] = {"normalized": key}
        if origin:
            data["origin"] = origin
        stored = self.repository.add(MemoryRecord(kind=MemoryKind.KNOWLEDGE, topic=TOPIC, content=clean, source=source, status=MemoryStatus.ACCEPTED, data=data))
        return next(item for item in self.all() if item.record.id == stored.id), True

    def switch(self, principle: Principle, *, on: bool) -> None:
        self.repository.set_status(principle.record.id, MemoryStatus.ACCEPTED if on else MemoryStatus.RETIRED)

    def edit(self, principle: Principle, text: str, *, source: str = "user") -> Principle:
        clean = " ".join(text.split())
        if not clean:
            raise KnowledgeError("The new wording is empty")
        replacement = MemoryRecord(kind=MemoryKind.KNOWLEDGE, topic=TOPIC, content=clean, source=source, status=principle.record.status, data={**principle.record.data, "normalized": normalize(clean)})
        stored = self.repository.supersede(principle.record.id, replacement)
        return next(item for item in self.all() if item.record.id == stored.id)


def principles_block(lines: Sequence[str]) -> str:
    kept = [line.strip() for line in lines if line and line.strip()]
    if not kept:
        return ""
    return "Working principles the user has set from past corrections (follow them):\n" + "\n".join(f"- {line}" for line in kept[:20])


__all__ = ["GAP_TOPIC", "Principle", "PrincipleService", "TOPIC", "normalize", "principles_block"]
