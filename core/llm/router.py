# File: core/llm/router.py

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

from core.llm.metrics import RequestMetric, RequestMetricsStore
from core.llm.models import LLMRequest, LLMResponse
from core.llm.ollama_client import OllamaClient, OllamaClientError

logger = logging.getLogger(__name__)

DEFAULT_PLANNING_TASKS: tuple[str, ...] = ("intent", "decision")

UNAVAILABLE_RETRY_SECONDS = 30.0


@dataclass(frozen=True)
class ModelRoutes:
    default: str
    tasks: dict[str, str] = field(default_factory=dict)
    fallbacks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    planning_tasks: tuple[str, ...] = DEFAULT_PLANNING_TASKS
    think: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ModelRoutes":
        default = str(config.get("model", "")).strip()
        section = config.get("models") if isinstance(config.get("models"), dict) else {}
        tasks_raw = section.get("tasks", {}) if isinstance(section.get("tasks"), dict) else {}
        tasks = {str(key).strip(): str(value).strip() for key, value in tasks_raw.items() if str(value).strip()}
        fallbacks_raw = section.get("fallbacks", {}) if isinstance(section.get("fallbacks"), dict) else {}
        fallbacks: dict[str, tuple[str, ...]] = {}
        for key, value in fallbacks_raw.items():
            chain = value if isinstance(value, list) else [value]
            fallbacks[str(key).strip()] = tuple(str(item).strip() for item in chain if str(item).strip())
        planning_raw = section.get("planning_tasks")
        planning = (
            tuple(str(item).strip() for item in planning_raw if str(item).strip())
            if isinstance(planning_raw, list)
            else DEFAULT_PLANNING_TASKS
        )
        think_raw = section.get("think", {}) if isinstance(section.get("think"), dict) else {}
        think = {str(key).strip(): bool(value) for key, value in think_raw.items()}
        if not default:
            default = tasks.get("chat", "")
        if not default:
            raise ValueError("No default model configured (config.json -> model)")
        return cls(default=default, tasks=tasks, fallbacks=fallbacks, planning_tasks=planning, think=think)

    def model_for(self, task: str | None) -> str:
        if task and task in self.tasks:
            return self.tasks[task]
        return self.default

    def candidates(self, model: str) -> list[str]:
        chain = [model]
        for item in self.fallbacks.get(model, ()):
            if item not in chain:
                chain.append(item)
        if self.default not in chain:
            chain.append(self.default)
        return chain


class ModelRouter:

    def __init__(
        self,
        client: OllamaClient,
        routes: ModelRoutes,
        metrics: RequestMetricsStore | None = None,
        request_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.client = client
        self.routes = routes
        self.metrics = metrics
        self.request_id_provider = request_id_provider
        self.last_response: LLMResponse | None = None
        self._available: list[str] | None = None
        self._unavailable_until = 0.0
        self._capabilities: dict[str, tuple[str, ...]] = {}


    @property
    def model(self) -> str:
        return self.routes.default

    def model_for(self, task: str | None) -> str:
        return self.routes.model_for(task)

    def generate(self, system_prompt: str, user_prompt: str, task: str | None = None) -> str:
        response = self.chat(LLMRequest.from_prompts(system_prompt, user_prompt, task=task))
        if not response.content.strip():
            raise OllamaClientError("Ollama response did not contain usable content")
        return response.content.strip()

    def _apply_task_options(self, request: LLMRequest) -> LLMRequest:
        if request.think is None and request.task in self.routes.think:
            return replace(request, think=self.routes.think[request.task])
        return request

    def chat(self, request: LLMRequest) -> LLMResponse:
        request = self._apply_task_options(request)
        requested = request.model or self.routes.model_for(request.task)
        if request.task in self.routes.planning_tasks and request.tools:
            requested = self._planning_model(requested)
        last_error: OllamaClientError | None = None
        for index, model in enumerate(self.routes.candidates(requested)):
            started = time.perf_counter()
            try:
                response = self.client.chat(replace(request, model=model))
            except OllamaClientError as error:
                wall = (time.perf_counter() - started) * 1000
                missing = self._is_model_missing(error)
                self._record(request, requested, model, None, wall, "model_missing" if missing else "error", str(error), index > 0)
                if not missing:
                    raise
                logger.warning("Model %s is not available (%s); trying the next candidate", model, error)
                last_error = error
                continue
            wall = (time.perf_counter() - started) * 1000
            self._record(request, requested, model, response, wall, "ok", None, index > 0)
            self.last_response = response
            return response
        raise OllamaClientError(
            f"No configured model is available for task {request.task or 'default'}: tried {', '.join(self.routes.candidates(requested))}"
        ) from last_error

    def chat_stream(self, request: LLMRequest) -> Iterator[str | LLMResponse]:
        request = self._apply_task_options(request)
        requested = request.model or self.routes.model_for(request.task)
        model = self._first_available(requested)
        started = time.perf_counter()
        try:
            for item in self.client.chat_stream(replace(request, model=model)):
                if isinstance(item, LLMResponse):
                    wall = (time.perf_counter() - started) * 1000
                    self._record(request, requested, model, item, wall, "ok", None, model != requested)
                    self.last_response = item
                yield item
        except OllamaClientError as error:
            wall = (time.perf_counter() - started) * 1000
            self._record(request, requested, model, None, wall, "error", str(error), model != requested)
            raise

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        chosen = model or self.routes.tasks.get("embedding") or self.routes.default
        started = time.perf_counter()
        try:
            vectors = self.client.embed(texts, model=chosen)
        except OllamaClientError as error:
            self._record_plain("embedding", chosen, chosen, (time.perf_counter() - started) * 1000, "error", str(error))
            raise
        self._record_plain("embedding", chosen, chosen, (time.perf_counter() - started) * 1000, "ok", None)
        return vectors


    def available_models(self, refresh: bool = False) -> list[str]:
        if self._available is None or refresh:
            if not refresh and time.monotonic() < self._unavailable_until:
                return []
            try:
                self._available = self.client.list_models()
            except OllamaClientError as error:
                logger.warning("Could not list models: %s", error)
                self._unavailable_until = time.monotonic() + UNAVAILABLE_RETRY_SECONDS
                return []
            self._unavailable_until = 0.0
        return list(self._available)

    def capabilities(self, model: str) -> tuple[str, ...]:
        if model not in self._capabilities:
            try:
                self._capabilities[model] = tuple(self.client.show_capabilities(model))
            except OllamaClientError:
                self._capabilities[model] = ()
        return self._capabilities[model]

    def check_routes(self) -> list[str]:
        available = self.available_models(refresh=True)
        if not available:
            return ["Ollama did not answer; model availability is unknown."]
        pulled = set(available)
        warnings: list[str] = []
        referenced: dict[str, list[str]] = {}
        referenced.setdefault(self.routes.default, []).append("default")
        for task, model in self.routes.tasks.items():
            referenced.setdefault(model, []).append(task)
        for model, chain in self.routes.fallbacks.items():
            for item in chain:
                referenced.setdefault(item, []).append(f"fallback for {model}")
        for model, uses in referenced.items():
            if not self._is_pulled(model, pulled):
                warnings.append(f"{model} ({', '.join(uses)}) is not pulled; run: ollama pull {model}")
        for task in self.routes.planning_tasks:
            model = self.routes.model_for(task)
            if self._is_pulled(model, pulled) and "tools" not in self.capabilities(model):
                warnings.append(f"{model} handles {task} but does not support tool-calling")
        embedding = self.routes.tasks.get("embedding")
        if embedding and self._is_pulled(embedding, pulled) and "embedding" not in self.capabilities(embedding):
            warnings.append(f"{embedding} is the embedding model but does not report the embedding capability")
        return warnings

    def status(self) -> dict[str, Any]:
        available = self.available_models()
        pulled = set(available)
        rows = [{"task": "default", "model": self.routes.default, "pulled": self._is_pulled(self.routes.default, pulled)}]
        for task, model in sorted(self.routes.tasks.items()):
            rows.append({"task": task, "model": model, "pulled": self._is_pulled(model, pulled)})
        return {
            "routes": rows,
            "fallbacks": {key: list(value) for key, value in self.routes.fallbacks.items()},
            "available": available,
            "planning_tasks": list(self.routes.planning_tasks),
        }


    def _planning_model(self, requested: str) -> str:
        available = self.available_models()
        if not available:
            return requested
        pulled = set(available)
        for candidate in self.routes.candidates(requested):
            if not self._is_pulled(candidate, pulled):
                continue
            if "tools" in self.capabilities(candidate):
                if candidate != requested:
                    logger.warning("%s cannot call tools; planning with %s instead", requested, candidate)
                return candidate
        logger.warning("No configured model reports tool-calling; planning with %s anyway", requested)
        return requested

    def _first_available(self, requested: str) -> str:
        available = self.available_models()
        if not available:
            return requested
        pulled = set(available)
        for candidate in self.routes.candidates(requested):
            if self._is_pulled(candidate, pulled):
                return candidate
        return requested

    @staticmethod
    def _is_pulled(model: str, pulled: set[str]) -> bool:
        if model in pulled:
            return True
        return ":" not in model and f"{model}:latest" in pulled

    @staticmethod
    def _is_model_missing(error: OllamaClientError) -> bool:
        text = str(error).lower()
        return "http 404" in text and "not found" in text

    def _record(
        self,
        request: LLMRequest,
        requested: str,
        model: str,
        response: LLMResponse | None,
        wall_ms: float,
        outcome: str,
        error: str | None,
        fallback: bool,
    ) -> None:
        if self.metrics is None:
            return
        usage = response.usage if response is not None else None
        metric = RequestMetric(
            task=request.task,
            requested_model=requested,
            model=model,
            outcome=outcome,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_duration_ms=usage.total_duration_ms if usage else 0.0,
            load_duration_ms=usage.load_duration_ms if usage else 0.0,
            wall_ms=wall_ms,
            tool_calls=tuple(call.name for call in response.tool_calls) if response is not None else (),
            error=error,
            fallback=fallback,
            request_id=self._request_id(),
        )
        self._safe_record(metric)

    def _record_plain(self, task: str, requested: str, model: str, wall_ms: float, outcome: str, error: str | None) -> None:
        if self.metrics is None:
            return
        self._safe_record(
            RequestMetric(task=task, requested_model=requested, model=model, outcome=outcome, wall_ms=wall_ms, error=error, request_id=self._request_id())
        )

    def _safe_record(self, metric: RequestMetric) -> None:
        try:
            self.metrics.record(metric)
        except Exception as error:
            logger.warning("Could not record model metrics: %s", error)

    def _request_id(self) -> str | None:
        if self.request_id_provider is None:
            return None
        try:
            return self.request_id_provider()
        except Exception:
            return None
