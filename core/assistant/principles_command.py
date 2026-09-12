# File: core/assistant/principles_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.knowledge.models import KnowledgeError
from core.knowledge.principles import PrincipleService

USAGE = "Usage: /principles | /principles add <text> | /principles off <n> | /principles on <n> | /principles edit <n> <text>"


class PrinciplesCommandHandler:
    def __init__(self, service: PrincipleService, output: OutputSink | None = None, actor: str = "user") -> None:
        self.service = service
        self.output = output
        self.actor = actor

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        if not text.lower().startswith("/principles"):
            return False
        rest = text[len("/principles"):].strip()
        words = rest.split()
        head = words[0].lower() if words else ""
        try:
            if not words:
                self._list()
            elif head == "add":
                self._add(rest[len(words[0]):].strip())
            elif head in {"off", "on"} and len(words) >= 2:
                self._switch(words[1], on=head == "on")
            elif head == "edit" and len(words) >= 3:
                self._edit(words[1], rest[len(words[0]) + 1 + len(words[1]):].strip())
            else:
                emit_output(self.output, USAGE)
        except KnowledgeError as error:
            emit_output(self.output, str(error), "error")
        return True

    def _list(self) -> None:
        items = self.service.all()
        if not items:
            emit_output(self.output, "No principles yet. /principles add <text>, or correct Iris three times the same way and one is made for you.")
            return
        on = sum(1 for item in items if item.on)
        lines = [f"{len(items)} principle{'s' if len(items) != 1 else ''}, {on} on. The ones that are on go into every prompt."]
        lines.extend(item.describe() for item in items)
        emit_output(self.output, "\n".join(lines))

    def _add(self, text: str) -> None:
        if not text:
            emit_output(self.output, USAGE)
            return
        principle, created = self.service.add(text, source=f"user:{self.actor}")
        emit_output(self.output, (f"Added principle {principle.number}: {principle.text}" if created else f"Already a principle ({principle.number}); it is on."))

    def _switch(self, reference: str, *, on: bool) -> None:
        principle = self.service.find(reference)
        if principle is None:
            emit_output(self.output, f"No principle {reference}; /principles lists them.", "error")
            return
        self.service.switch(principle, on=on)
        emit_output(self.output, f"Principle {principle.number} is now {'on' if on else 'off'}: {principle.text}")

    def _edit(self, reference: str, text: str) -> None:
        principle = self.service.find(reference)
        if principle is None:
            emit_output(self.output, f"No principle {reference}; /principles lists them.", "error")
            return
        updated = self.service.edit(principle, text, source=f"user:{self.actor}")
        emit_output(self.output, f"Principle {updated.number} now reads: {updated.text} (the old wording stays as history)")


__all__ = ["PrinciplesCommandHandler", "USAGE"]
