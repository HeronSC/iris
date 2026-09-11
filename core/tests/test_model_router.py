# File: core/tests/test_model_router.py

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.assistant.models_command import ModelsCommandHandler
from core.llm.metrics import RequestMetric, RequestMetricsStore
from core.llm.models import LLMRequest, LLMResponse, LLMUsage, ToolCall
from core.llm.ollama_client import OllamaClientError
from core.llm.router import ModelRouter, ModelRoutes
from core.storage.sqlite_database import SQLiteDatabase


class FakeClient:
    """Stands in for OllamaClient: answers per model, or fails per model."""

    def __init__(self, missing: set[str] | None = None, down: bool = False) -> None:
        self.missing = missing or set()
        self.down = down
        self.calls: list[LLMRequest] = []
        self.pulled = ["qwen3:8b", "qwen2.5-coder:7b", "nomic-embed-text:latest"]

    def chat(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.down:
            raise OllamaClientError("Ollama connection failed: refused")
        if request.model in self.missing:
            raise OllamaClientError(f"Ollama returned HTTP 404: model '{request.model}' not found")
        tool_calls = (ToolCall(name="launch_application", arguments={}),) if request.tools else ()
        return LLMResponse(
            content=f"answer from {request.model}",
            tool_calls=tool_calls,
            model=request.model or "",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_duration_ms=100.0),
        )

    def chat_stream(self, request: LLMRequest):
        yield "hel"
        yield "lo"
        yield LLMResponse(content="hello", model=request.model or "", usage=LLMUsage(prompt_tokens=3, completion_tokens=2))

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        self.calls.append(LLMRequest(messages=(), model=model, task="embedding"))
        return [[0.1, 0.2] for _ in texts]

    def list_models(self) -> list[str]:
        if self.down:
            raise OllamaClientError("Ollama connection failed")
        return list(self.pulled)

    def show_capabilities(self, model: str) -> list[str]:
        return {
            "qwen3:8b": ["completion", "tools", "thinking"],
            "qwen2.5-coder:7b": ["completion", "tools"],
            "nomic-embed-text": ["embedding"],
            "gemma3:4b": ["completion"],
        }.get(model, [])


CONFIG: dict[str, Any] = {
    "model": "qwen3:8b",
    "models": {
        "tasks": {"code": "qwen2.5-coder:7b", "embedding": "nomic-embed-text", "vision": "qwen2.5vl:7b"},
        "fallbacks": {"qwen3:8b": ["mistral-small:24b"]},
    },
}


class RoutesTests(unittest.TestCase):
    def test_from_config_reads_default_tasks_and_fallbacks(self) -> None:
        routes = ModelRoutes.from_config(CONFIG)
        self.assertEqual(routes.default, "qwen3:8b")
        self.assertEqual(routes.model_for("code"), "qwen2.5-coder:7b")
        self.assertEqual(routes.model_for("chat"), "qwen3:8b")
        self.assertEqual(routes.model_for(None), "qwen3:8b")
        self.assertEqual(routes.candidates("qwen3:8b"), ["qwen3:8b", "mistral-small:24b"])
        self.assertEqual(routes.candidates("qwen2.5-coder:7b"), ["qwen2.5-coder:7b", "qwen3:8b"])
        self.assertEqual(routes.planning_tasks, ("intent", "decision"))

    def test_plain_model_key_still_works_without_a_models_block(self) -> None:
        routes = ModelRoutes.from_config({"model": "qwen3:8b"})
        self.assertEqual(routes.model_for("code"), "qwen3:8b")
        with self.assertRaises(ValueError):
            ModelRoutes.from_config({})


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.metrics = RequestMetricsStore(SQLiteDatabase(Path(self.tempdir.name) / "metrics.db"))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _router(self, client: FakeClient) -> ModelRouter:
        return ModelRouter(client, ModelRoutes.from_config(CONFIG), metrics=self.metrics)

    def test_routes_by_task_and_records_usage(self) -> None:
        client = FakeClient()
        router = self._router(client)
        self.assertEqual(router.generate("s", "u", task="code"), "answer from qwen2.5-coder:7b")
        self.assertEqual(router.generate("s", "u", task="chat"), "answer from qwen3:8b")
        self.assertEqual([call.model for call in client.calls], ["qwen2.5-coder:7b", "qwen3:8b"])

        rows = self.metrics.recent()
        self.assertEqual([(row["task"], row["model"], row["outcome"]) for row in rows], [("chat", "qwen3:8b", "ok"), ("code", "qwen2.5-coder:7b", "ok")])
        self.assertEqual(rows[0]["prompt_tokens"], 10)
        self.assertGreaterEqual(rows[0]["wall_ms"], 0.0)

    def test_per_task_think_setting_applies_unless_the_request_sets_it(self) -> None:
        client = FakeClient()
        routes = ModelRoutes.from_config({**CONFIG, "models": {**CONFIG["models"], "think": {"decision": False}}})
        router = ModelRouter(client, routes, metrics=self.metrics)
        router.chat(LLMRequest.from_prompts("s", "u", task="decision"))
        self.assertEqual(client.calls[-1].think, False)
        router.chat(LLMRequest.from_prompts("s", "u", task="chat"))
        self.assertIsNone(client.calls[-1].think, "an unlisted task keeps the model default")
        router.chat(LLMRequest.from_prompts("s", "u", task="decision", think=True))
        self.assertEqual(client.calls[-1].think, True, "the request's own setting wins")
        list(router.chat_stream(LLMRequest.from_prompts("s", "u", task="decision")))

    def test_explicit_model_on_the_request_wins(self) -> None:
        client = FakeClient()
        router = self._router(client)
        router.chat(LLMRequest.from_prompts("s", "u", task="chat", model="qwen2.5-coder:7b"))
        self.assertEqual(client.calls[-1].model, "qwen2.5-coder:7b")

    def test_missing_model_falls_back_along_the_chain(self) -> None:
        client = FakeClient(missing={"qwen3:8b"})
        router = self._router(client)
        response = router.chat(LLMRequest.from_prompts("s", "u", task="chat"))
        self.assertEqual(response.model, "mistral-small:24b")
        self.assertEqual([call.model for call in client.calls], ["qwen3:8b", "mistral-small:24b"])
        rows = self.metrics.recent()
        self.assertEqual([(row["model"], row["outcome"], row["fallback"]) for row in rows], [("mistral-small:24b", "ok", True), ("qwen3:8b", "model_missing", False)])

    def test_every_candidate_missing_is_a_clear_error(self) -> None:
        client = FakeClient(missing={"qwen3:8b", "mistral-small:24b"})
        router = self._router(client)
        with self.assertRaises(OllamaClientError) as error:
            router.generate("s", "u", task="chat")
        self.assertIn("No configured model is available", str(error.exception))
        self.assertIn("mistral-small:24b", str(error.exception))

    def test_host_down_does_not_try_fallbacks(self) -> None:
        client = FakeClient(down=True)
        router = self._router(client)
        with self.assertRaises(OllamaClientError):
            router.generate("s", "u", task="chat")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.metrics.recent()[0]["outcome"], "error")

    def test_tool_calls_are_recorded_by_name(self) -> None:
        router = self._router(FakeClient())
        from core.llm.models import ToolSpec

        response = router.chat(LLMRequest.from_prompts("s", "u", task="intent", tools=(ToolSpec("launch_application", "x", {"type": "object"}),)))
        self.assertTrue(response.has_tool_calls)
        self.assertEqual(self.metrics.recent()[0]["tool_calls"], ["launch_application"])

    def test_embed_uses_the_embedding_route(self) -> None:
        client = FakeClient()
        router = self._router(client)
        vectors = router.embed(["a", "b"])
        self.assertEqual(len(vectors), 2)
        self.assertEqual(client.calls[-1].model, "nomic-embed-text")
        self.assertEqual(self.metrics.recent()[0]["task"], "embedding")

    def test_stream_records_the_final_response(self) -> None:
        router = self._router(FakeClient())
        items = list(router.chat_stream(LLMRequest.from_prompts("s", "u", task="chat")))
        self.assertEqual(items[:2], ["hel", "lo"])
        self.assertIsInstance(items[-1], LLMResponse)
        self.assertEqual(self.metrics.recent()[0]["completion_tokens"], 2)

    def test_check_routes_reports_unpulled_and_unsuitable_models(self) -> None:
        client = FakeClient()
        client.pulled = ["qwen3:8b", "qwen2.5-coder:7b", "nomic-embed-text:latest", "gemma3:4b"]
        routes = ModelRoutes.from_config({"model": "gemma3:4b", "models": {"tasks": {"vision": "qwen2.5vl:7b", "embedding": "nomic-embed-text"}}})
        router = ModelRouter(client, routes, metrics=None)
        warnings = router.check_routes()
        self.assertTrue(any("qwen2.5vl:7b" in item and "not pulled" in item for item in warnings), warnings)
        self.assertTrue(any("gemma3:4b" in item and "tool-calling" in item for item in warnings), warnings)
        self.assertFalse(any("nomic-embed-text" in item for item in warnings), warnings)

        self.assertEqual(ModelRouter(FakeClient(down=True), routes).check_routes(), ["Ollama did not answer; model availability is unknown."])

    def test_metrics_failure_never_breaks_a_call(self) -> None:
        class BrokenStore:
            def record(self, metric: RequestMetric) -> None:
                raise RuntimeError("disk full")

        router = ModelRouter(FakeClient(), ModelRoutes.from_config(CONFIG), metrics=BrokenStore())  # type: ignore[arg-type]
        self.assertEqual(router.generate("s", "u"), "answer from qwen3:8b")


class MetricsStoreTests(unittest.TestCase):
    def test_summary_groups_by_task_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RequestMetricsStore(SQLiteDatabase(Path(tmp) / "m.db"))
            for _ in range(3):
                store.record(RequestMetric(task="chat", requested_model="qwen3:8b", model="qwen3:8b", outcome="ok", prompt_tokens=100, completion_tokens=20, wall_ms=200))
            store.record(RequestMetric(task="intent", requested_model="qwen3:8b", model="qwen3:8b", outcome="ok", prompt_tokens=50, completion_tokens=5, wall_ms=800, tool_calls=("git_status",)))
            store.record(RequestMetric(task="chat", requested_model="qwen3:8b", model="mistral-small:24b", outcome="error", error="boom", fallback=True))
            rows = store.summary()
            self.assertEqual(rows[0]["task"], "chat")
            self.assertEqual(rows[0]["model"], "qwen3:8b")
            self.assertEqual(rows[0]["calls"], 3)
            self.assertEqual(rows[0]["prompt_tokens"], 300)
            self.assertAlmostEqual(rows[0]["avg_wall_ms"], 200.0)
            errors = {(row["task"], row["model"]): row["errors"] for row in rows}
            self.assertEqual(errors[("chat", "mistral-small:24b")], 1)
            self.assertEqual(store.count(), 5)
            self.assertEqual(store.summary(hours=1)[0]["calls"], 3)
            self.assertEqual(store.recent(1)[0]["error"], "boom")


class ModelsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.metrics = RequestMetricsStore(SQLiteDatabase(Path(self.tempdir.name) / "metrics.db"))
        self.router = ModelRouter(FakeClient(), ModelRoutes.from_config(CONFIG), metrics=self.metrics)
        self.output: list[str] = []
        self.handler = ModelsCommandHandler(self.router, self.metrics, output=lambda text, role=None: self.output.append(text))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_routes_listing(self) -> None:
        self.assertFalse(self.handler.handle("/modelsx", {}))
        self.assertTrue(self.handler.handle("/models", {}))
        text = self.output[-1]
        self.assertIn("- default: qwen3:8b", text)
        self.assertIn("- code: qwen2.5-coder:7b", text)
        self.assertIn("- vision: qwen2.5vl:7b  (not pulled)", text)
        self.assertIn("qwen3:8b -> mistral-small:24b", text)
        self.assertIn("Warnings:", text)

    def test_usage_and_recent(self) -> None:
        self.assertTrue(self.handler.handle("/models usage", {}))
        self.assertIn("No model calls recorded", self.output[-1])
        self.router.generate("s", "u", task="chat")
        self.router.generate("s", "u", task="code")
        self.assertTrue(self.handler.handle("/models usage 24", {}))
        self.assertIn("last 24 hours", self.output[-1])
        self.assertIn("chat on qwen3:8b: 1 calls, 10+5 tokens", self.output[-1])
        self.assertIn("Total: 2 calls, 20+10 tokens", self.output[-1])
        self.assertTrue(self.handler.handle("/models recent 1", {}))
        self.assertIn("Last 1 model calls:", self.output[-1])
        self.assertIn("code -> qwen2.5-coder:7b", self.output[-1])


if __name__ == "__main__":
    unittest.main()
