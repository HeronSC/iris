# File: core/tools/registry.py

from __future__ import annotations

from typing import Any, Iterable

from core.llm.models import ToolSpec
from core.tools.models import ToolDefinition, ToolKind


class ToolRegistry:
    """Every tool Iris can run, declared once.

    The registry does not run anything. It answers three questions: what tools
    exist, which of them the model may choose from, and what a given tool's
    arguments and permissions are. Execution stays with the executor (actions),
    the command handlers (commands), the knowledge router (capabilities), and
    the MCP client (MCP tools).
    """

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._disabled: set[str] = set()

    # -- declaration ------------------------------------------------------

    def register(self, definition: ToolDefinition, replace: bool = False) -> None:
        if definition.name in self._definitions and not replace:
            raise ValueError(f"Tool already registered: {definition.name}")
        if definition.is_facet and definition.action not in self._definitions:
            raise ValueError(f"Tool {definition.name} binds to unknown action {definition.action}")
        self._definitions[definition.name] = definition

    def register_all(self, definitions: Iterable[ToolDefinition], replace: bool = False) -> None:
        for definition in definitions:
            self.register(definition, replace=replace)

    def unregister(self, name: str) -> None:
        self._definitions.pop(name, None)
        self._disabled.discard(name)

    # -- lookup -----------------------------------------------------------

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._definitions

    def names(self, kind: ToolKind | None = None) -> list[str]:
        return [item.name for item in self.definitions(kind=kind)]

    def definitions(self, kind: ToolKind | None = None, enabled_only: bool = False) -> list[ToolDefinition]:
        items = sorted(self._definitions.values(), key=lambda item: item.name)
        if kind is not None:
            items = [item for item in items if item.kind == kind]
        if enabled_only:
            items = [item for item in items if self.is_enabled(item.name)]
        return items

    def handler(self, name: str) -> Any | None:
        definition = self._definitions.get(name)
        return definition.handler if definition is not None else None

    # -- enable / disable -------------------------------------------------

    def disable(self, name: str) -> None:
        if name not in self._definitions:
            raise KeyError(name)
        self._disabled.add(name)

    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    def is_enabled(self, name: str) -> bool:
        definition = self._definitions.get(name)
        if definition is None or name in self._disabled:
            return False
        if definition.is_facet and definition.action in self._disabled:
            return False
        return True

    # -- what the model sees ----------------------------------------------

    def model_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(
            item.to_spec()
            for item in self.definitions(enabled_only=True)
            if item.expose_to_model
        )

    def describe(self) -> list[dict[str, Any]]:
        """A plain listing for /tools style inspection."""
        rows: list[dict[str, Any]] = []
        for item in self.definitions():
            rows.append(
                {
                    "name": item.name,
                    "kind": item.kind.value,
                    "description": item.description,
                    "permission": item.permission.value,
                    "requires_confirmation": item.requires_confirmation,
                    "version": item.version,
                    "enabled": self.is_enabled(item.name),
                    "exposed": item.expose_to_model,
                    "source": item.source,
                    "action": item.action,
                }
            )
        return rows
