# File: core/assistant/llm_client.py

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from core.llm.models import LLMRequest, LLMResponse


class LLMClient(Protocol):

    def generate(self, system_prompt: str, user_prompt: str, task: str | None = None) -> str:
        ...

    def chat(self, request: LLMRequest) -> LLMResponse:
        ...

    def chat_stream(self, request: LLMRequest) -> Iterator[str | LLMResponse]:
        ...
