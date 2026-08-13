from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output


class SaveCommandHandler:
    def __init__(self, output: OutputSink | None = None) -> None:
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        if user_input.strip().lower() != "/save":
            return False

        writer = state.get("writer")
        store = state.get("store")

        if writer is None or store is None or not hasattr(store, "to_dict"):
            emit_output(self.output, "Save is unavailable: memory components are not initialized.")
            return True

        memory_data = store.to_dict()
        writer.save_profile(memory_data.get("profile", {}))
        writer.save_preferences(memory_data.get("preferences", {}))
        writer.save_projects(memory_data.get("projects", {}))
        writer.save_knowledge(memory_data.get("knowledge", {}))
        emit_output(self.output, "Memory snapshot saved.")
        return True
