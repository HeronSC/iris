# File: core/llm/ollama_client.py

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import ollama

from core.llm.models import LLMRequest, LLMResponse, LLMUsage, ToolCall


class OllamaClientError(Exception):
    pass


UsageListener = Callable[[LLMRequest, LLMResponse], None]


PROBE_TIMEOUT_SECONDS = 3.0


class OllamaClient:

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float | None = 30.0,
        usage_listener: UsageListener | None = None,
        probe_timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.probe_timeout_seconds = probe_timeout_seconds
        self.usage_listener = usage_listener
        self.last_response: LLMResponse | None = None
        self._client = ollama.Client(host=self.base_url, timeout=timeout_seconds)
        self._probe = ollama.Client(host=self.base_url, timeout=probe_timeout_seconds)


    def generate(self, system_prompt: str, user_prompt: str, task: str | None = None) -> str:
        response = self.chat(LLMRequest.from_prompts(system_prompt, user_prompt, task=task))
        if not response.content.strip():
            raise OllamaClientError("Ollama response did not contain usable content")
        return response.content.strip()


    def chat(self, request: LLMRequest) -> LLMResponse:
        raw = self._call(lambda: self._client.chat(**self._chat_kwargs(request)))
        response = self._parse_response(raw)
        if not response.content.strip() and not response.tool_calls:
            raise OllamaClientError("Ollama returned an empty response")
        self._record(request, response)
        return response

    def chat_stream(self, request: LLMRequest) -> Iterator[str | LLMResponse]:
        stream = self._call(lambda: self._client.chat(**self._chat_kwargs(request), stream=True))
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        final_chunk: Any = None
        try:
            for chunk in stream:
                final_chunk = chunk
                message = getattr(chunk, "message", None)
                delta = getattr(message, "content", "") or ""
                if delta:
                    content_parts.append(delta)
                    yield delta
                thinking = getattr(message, "thinking", None)
                if thinking:
                    thinking_parts.append(thinking)
                tool_calls.extend(self._parse_tool_calls(message))
        except (ollama.ResponseError, httpx.HTTPError, OSError) as error:
            raise self._wrap_error(error) from error
        response = LLMResponse(
            content="".join(content_parts),
            tool_calls=tuple(tool_calls),
            model=str(getattr(final_chunk, "model", "") or self.model),
            usage=self._parse_usage(final_chunk),
            done_reason=getattr(final_chunk, "done_reason", None),
            thinking="".join(thinking_parts) or None,
        )
        self._record(request, response)
        yield response

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        raw = self._call(lambda: self._client.embed(model=model or self.model, input=texts))
        embeddings = getattr(raw, "embeddings", None) or []
        return [list(vector) for vector in embeddings]


    def list_models(self) -> list[str]:
        raw = self._call(self._probe.list)
        names: list[str] = []
        for item in getattr(raw, "models", None) or []:
            name = getattr(item, "model", None) or getattr(item, "name", None)
            if name:
                names.append(str(name))
        return names

    def show_capabilities(self, model: str) -> list[str]:
        raw = self._call(lambda: self._client.show(model))
        capabilities = getattr(raw, "capabilities", None) or []
        return [str(item) for item in capabilities]


    def _chat_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [message.to_ollama() for message in request.messages],
        }
        if request.tools:
            kwargs["tools"] = [tool.to_ollama() for tool in request.tools]
        if request.format is not None:
            kwargs["format"] = request.format
        if request.think is not None:
            kwargs["think"] = request.think
        if request.options:
            kwargs["options"] = dict(request.options)
        return kwargs

    def _call(self, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except (ollama.ResponseError, httpx.HTTPError, OSError) as error:
            raise self._wrap_error(error) from error

    def _wrap_error(self, error: Exception) -> OllamaClientError:
        if isinstance(error, ollama.ResponseError):
            status = getattr(error, "status_code", None)
            detail = getattr(error, "error", None) or str(error)
            return OllamaClientError(f"Ollama returned HTTP {status}: {detail}")
        if isinstance(error, httpx.TimeoutException):
            return OllamaClientError(f"Ollama request timed out: {error}")
        if isinstance(error, httpx.HTTPStatusError):
            body = error.response.text if error.response is not None else ""
            return OllamaClientError(f"Ollama returned HTTP {error.response.status_code}: {body}")
        if isinstance(error, (httpx.HTTPError, OSError)):
            text = str(error).lower()
            if "timed out" in text or "timeout" in text:
                return OllamaClientError(f"Ollama request timed out: {error}")
            return OllamaClientError(f"Ollama connection failed: {error}")
        return OllamaClientError(str(error))

    def _parse_response(self, raw: Any) -> LLMResponse:
        message = getattr(raw, "message", None)
        content = getattr(message, "content", "") if message is not None else ""
        if not isinstance(content, str):
            content = ""
        thinking = getattr(message, "thinking", None) if message is not None else None
        return LLMResponse(
            content=content,
            tool_calls=tuple(self._parse_tool_calls(message)),
            model=str(getattr(raw, "model", "") or self.model),
            usage=self._parse_usage(raw),
            done_reason=getattr(raw, "done_reason", None),
            thinking=thinking if isinstance(thinking, str) and thinking else None,
        )

    @staticmethod
    def _parse_tool_calls(message: Any) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for item in getattr(message, "tool_calls", None) or []:
            function = getattr(item, "function", None)
            if function is None and isinstance(item, dict):
                function = item.get("function")
            if isinstance(function, dict):
                name = function.get("name")
                arguments = function.get("arguments")
            else:
                name = getattr(function, "name", None)
                arguments = getattr(function, "arguments", None)
            if not name:
                continue
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ToolCall(name=str(name), arguments=dict(arguments)))
        return calls

    @staticmethod
    def _parse_usage(raw: Any) -> LLMUsage:
        def _int(name: str) -> int:
            value = getattr(raw, name, None)
            return int(value) if isinstance(value, (int, float)) else 0

        return LLMUsage(
            prompt_tokens=_int("prompt_eval_count"),
            completion_tokens=_int("eval_count"),
            total_duration_ms=_int("total_duration") / 1_000_000,
            load_duration_ms=_int("load_duration") / 1_000_000,
        )

    def _record(self, request: LLMRequest, response: LLMResponse) -> None:
        self.last_response = response
        if self.usage_listener is not None:
            try:
                self.usage_listener(request, response)
            except Exception:
                pass
