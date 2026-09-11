# File: core/llm/models.py

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_name: str | None = None
    images: tuple[bytes, ...] = ()

    def to_ollama(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.images:
            payload["images"] = [base64.b64encode(image).decode("ascii") for image in self.images]
        if self.tool_calls:
            payload["tool_calls"] = [
                {"function": {"name": call.name, "arguments": dict(call.arguments)}} for call in self.tool_calls
            ]
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        return payload


@dataclass(frozen=True)
class ToolSpec:

    name: str
    description: str
    parameters: dict[str, Any]

    def to_ollama(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class LLMRequest:
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    model: str | None = None
    format: Literal["json"] | dict[str, Any] | None = None
    think: bool | None = None
    options: dict[str, Any] = field(default_factory=dict)
    task: str | None = None

    @classmethod
    def from_prompts(cls, system_prompt: str, user_prompt: str, **kwargs: Any) -> "LLMRequest":
        return cls(
            messages=(
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ),
            **kwargs,
        )


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_duration_ms: float = 0.0
    load_duration_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    done_reason: str | None = None
    thinking: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
