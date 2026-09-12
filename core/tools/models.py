# File: core/tools/models.py


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
    pass


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
    outbound: bool = False
    irreversible: bool = False
    source: str = "native"
    handler: Any = None
    keywords: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    cost: str = ""

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

    def validate_arguments(self, raw: dict[str, Any] | None) -> dict[str, Any]:
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
