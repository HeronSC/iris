# File: core/tools/models.py

"""The one tool definition.

A ``ToolDefinition`` is the only place a tool is declared: what it is called,
what it does, what arguments it takes (a pydantic model, or a raw JSON Schema
for tools that arrive with one, such as MCP servers), what permission level it
needs, and whether a user has to confirm it before it runs.

Native actions, the knowledge providers, and MCP tools all register the same
shape. The model sees the definition through ``to_spec()``; the executor
enforces it through ``requires_confirmation`` and ``permission``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError

from core.llm.models import ToolSpec


class PermissionLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class ToolKind(str, Enum):
    ACTION = "action"          # runs through core/actions/executor.py
    COMMAND = "command"        # a slash-command handler exposed to the model
    CAPABILITY = "capability"  # a knowledge provider (weather, news, recall, ...)
    MCP = "mcp"                # a tool served by an MCP server


class ToolArgumentError(ValueError):
    """Raised when a tool's arguments do not satisfy its declared schema."""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    arguments: type[BaseModel] | None = None
    parameters: dict[str, Any] | None = None
    permission: PermissionLevel = PermissionLevel.READ
    requires_confirmation: bool = False
    kind: ToolKind = ToolKind.ACTION
    action: str | None = None
    bind: dict[str, Any] = field(default_factory=dict)
    version: str = "1"
    expose_to_model: bool = True
    #: True when running the tool sends something off this machine. Section 10
    #: caps outbound work, and it can only do that if a tool says it is outbound
    #: rather than the policy keeping a list of tool names that will go stale.
    outbound: bool = False
    source: str = "native"
    handler: Any = None
    #: Words or short phrases in a request that make this tool worth offering to
    #: the model. The router's gate checks them (whole words, singular or plural)
    #: before spending a model call; the tool's name words count automatically.
    keywords: tuple[str, ...] = ()

    # -- schema -----------------------------------------------------------

    def parameters_schema(self) -> dict[str, Any]:
        if self.arguments is not None:
            schema = self.arguments.model_json_schema()
            schema.pop("title", None)
            for prop in schema.get("properties", {}).values():
                if isinstance(prop, dict):
                    prop.pop("title", None)
            return schema
        if self.parameters is not None:
            return dict(self.parameters)
        return {"type": "object", "properties": {}}

    def to_spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters_schema())

    # -- arguments ---------------------------------------------------------

    def validate_arguments(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        """Coerce ``raw`` to the declared shape and merge any bound arguments.

        Returns a plain dict ready for an ``ActionRequest``. Raises
        ``ToolArgumentError`` when the arguments do not fit.
        """
        payload = dict(raw or {})
        if self.arguments is not None:
            try:
                model = self.arguments.model_validate(payload)
            except ValidationError as error:
                raise ToolArgumentError(self._format_validation_error(error)) from error
            payload = model.model_dump(mode="json", exclude_unset=False)
        merged = dict(payload)
        merged.update(self.bind)
        return merged

    def with_handler(self, handler: Any) -> "ToolDefinition":
        return replace(self, handler=handler)

    @property
    def target_action(self) -> str:
        """The action the executor runs: the bound action for a facet, else this tool."""
        return self.action or self.name

    @property
    def is_facet(self) -> bool:
        return self.action is not None and self.action != self.name

    @staticmethod
    def _format_validation_error(error: ValidationError) -> str:
        parts: list[str] = []
        for item in error.errors():
            location = ".".join(str(piece) for piece in item.get("loc", ())) or "arguments"
            parts.append(f"{location}: {item.get('msg', 'invalid')}")
        return "; ".join(parts) or "invalid arguments"
