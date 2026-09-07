# File: core/assistant/__init__.py

from typing import Any

__all__ = ["AssistantCoordinator"]


def __getattr__(name: str) -> Any:
	if name == "AssistantCoordinator":
		#! @allow-local-import
		from core.assistant.coordinator import AssistantCoordinator

		return AssistantCoordinator
	raise AttributeError(name)
