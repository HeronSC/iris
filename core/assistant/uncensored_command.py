# File: core/assistant/uncensored_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output

USAGE = "Usage: /uncensored | /uncensored on | /uncensored off"


class UncensoredCommandHandler:

    def __init__(self, router: Any, output: OutputSink | None = None) -> None:
        self.router = router
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        lowered = text.lower()
        if lowered != "/uncensored" and not lowered.startswith("/uncensored "):
            return False
        parts = text.split()
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action == "status":
            self._status()
            return True
        if action in {"on", "off"}:
            self._switch(action == "on")
            return True
        emit_output(self.output, USAGE)
        return True

    def _target(self) -> str:
        routes = getattr(self.router, "routes", None)
        return str(getattr(routes, "on_refusal", "") or "")

    def _current(self) -> str:
        return str(getattr(self.router, "force_model", "") or "")

    def _status(self) -> None:
        current = self._current()
        if current:
            emit_output(self.output, f"Uncensored mode is on; every reply comes from {current}. /uncensored off to stop.")
            return
        target = self._target()
        if not target:
            emit_output(self.output, "Uncensored mode is off, and no model is set for it (config.json -> models.on_refusal).")
            return
        emit_output(self.output, f"Uncensored mode is off. Refusals still reroute to {target}. /uncensored on pins every reply to it.")

    def _switch(self, on: bool) -> None:
        if not on:
            if not self._current():
                emit_output(self.output, "Uncensored mode was already off.")
                return
            self.router.force_model = ""
            emit_output(self.output, "Uncensored mode off; back to normal routing.")
            return
        target = self._target()
        if not target:
            emit_output(self.output, "No model is set for uncensored mode (config.json -> models.on_refusal).", "error")
            return
        self.router.force_model = target
        emit_output(
            self.output,
            f"Uncensored mode on; every reply comes from {target}. Tools and Iris's usual framing are off until /uncensored off.",
        )


__all__ = ["USAGE", "UncensoredCommandHandler"]
