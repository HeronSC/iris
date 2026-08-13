from typing import Any

__all__ = ["AssistantCoordinator"]


def __getattr__(name: str) -> Any:
	if name == "AssistantCoordinator":
		from .coordinator import AssistantCoordinator

		return AssistantCoordinator
	raise AttributeError(name)
