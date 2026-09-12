# File: core/assistant/stop_command.py

from __future__ import annotations

from typing import Any, Callable

from core.assistant.output import OutputSink, emit_output


class StopCommandHandler:

    def __init__(self, halt: Callable[[], list[str]], release: Callable[[], list[str]], output: OutputSink | None = None) -> None:
        self.halt = halt
        self.release = release
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        lowered = user_input.strip().lower()
        if lowered in {"/stop", "/halt"}:
            halted = self.halt()
            emit_output(self.output, "Stopped: " + (", ".join(halted) if halted else "nothing was running") + ". /resume starts things again.")
            return True
        if lowered == "/resume":
            released = self.release()
            emit_output(self.output, "Resumed: " + (", ".join(released) if released else "nothing was stopped") + ".")
            return True
        return False
