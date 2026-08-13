from __future__ import annotations

from typing import Protocol

from core.actions.models import ActionRequest, ActionResult, ValidationResult


class Action(Protocol):
    @property
    def name(self) -> str:
        ...

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        ...

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        ...


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    def register(self, action: Action, replace: bool = False) -> None:
        if action.name in self._actions and not replace:
            raise ValueError(f"Action already registered: {action.name}")
        self._actions[action.name] = action

    def get(self, name: str) -> Action | None:
        return self._actions.get(name)

    def names(self) -> list[str]:
        return sorted(self._actions.keys())

