# File: core/assistant/context_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.context.service import ContextService

USAGE = "Usage: /context | /context history | /context pause | /context resume"


class ContextCommandHandler:

    def __init__(self, service: ContextService | None, output: OutputSink | None = None) -> None:
        self.service = service
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        stripped = user_input.strip()
        if not stripped.lower().startswith("/context"):
            return False
        parts = stripped.split()
        subcommand = parts[1].lower() if len(parts) > 1 else ""
        if self.service is None:
            emit_output(self.output, "Context capture is not available in this host.")
            return True
        if subcommand == "":
            emit_output(self.output, self._current_text())
            return True
        if subcommand == "history":
            emit_output(self.output, self._history_text())
            return True
        if subcommand == "pause":
            self.service.pause()
            emit_output(self.output, "Context capture paused. Iris will not look at which window is active until /context resume.")
            return True
        if subcommand == "resume":
            self.service.resume()
            emit_output(self.output, "Context capture resumed.")
            return True
        emit_output(self.output, USAGE)
        return True

    def _current_text(self) -> str:
        if self.service.paused:
            return "Context capture is paused. /context resume turns it back on."
        current = self.service.current()
        if current is None:
            return "No window has been seen yet."
        lines = [f"Now: {current.describe()}"]
        previous = self.service.previous()
        if previous is not None:
            lines.append(f"Before that: {previous.describe()}")
        if self.service.last_error:
            lines.append(f"Last provider error: {self.service.last_error}")
        return "\n".join(lines)

    def _history_text(self) -> str:
        entries = self.service.history()
        if not entries:
            return "No windows have been seen yet."
        return "Recent windows, newest first:\n" + "\n".join(f"{index}. {entry.describe()} ({entry.captured_at[11:16]} UTC)" for index, entry in enumerate(entries, start=1))


__all__ = ["ContextCommandHandler", "USAGE"]
