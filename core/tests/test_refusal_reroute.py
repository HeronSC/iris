# File: core/tests/test_refusal_reroute.py

from __future__ import annotations

import unittest

from core.assistant.uncensored_command import UncensoredCommandHandler
from core.llm.models import ChatMessage, LLMRequest, LLMResponse, ToolCall, ToolSpec
from core.llm.refusal import looks_like_refusal
from core.llm.router import DEFAULT_REROUTE_SYSTEM, ModelRouter, ModelRoutes

PRIMARY = "qwen3:8b"
TARGET = "magnum:22b"
REFUSAL = "I can't generate explicit or sexually explicit content, but I would be happy to help with something else."
ANSWER = "She stepped into the dim hallway and listened while the house settled again into quiet."
TOOLS = (ToolSpec(name="search", description="find things", parameters={}),)


class FakeClient:

    def __init__(self, tool_call: bool = False) -> None:
        self.tool_call = tool_call
        self.requests: list[LLMRequest] = []

    def _body(self, model: str | None) -> str:
        return REFUSAL if model == PRIMARY else ANSWER

    def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.model == PRIMARY and self.tool_call:
            return LLMResponse(content="", tool_calls=(ToolCall(name="search", arguments={}),))
        return LLMResponse(content=self._body(request.model))

    def chat_stream(self, request: LLMRequest):
        self.requests.append(request)
        if request.model == PRIMARY and self.tool_call:
            yield LLMResponse(content="", tool_calls=(ToolCall(name="search", arguments={}),))
            return
        body = self._body(request.model)
        for index in range(0, len(body), 12):
            yield body[index:index + 12]
        yield LLMResponse(content=body)

    def list_models(self) -> list[str]:
        return [PRIMARY, TARGET]

    def show_capabilities(self, model: str) -> tuple[str, ...]:
        return ("tools",)

    @property
    def models_used(self) -> list[str | None]:
        return [request.model for request in self.requests]


def routes(on_refusal: str = TARGET) -> ModelRoutes:
    return ModelRoutes(default=PRIMARY, on_refusal=on_refusal)


def graph_request() -> LLMRequest:
    messages = (
        ChatMessage(role="system", content="You are Iris, a personal assistant."),
        ChatMessage(role="user", content="write one"),
        ChatMessage(role="assistant", content="a tame story"),
        ChatMessage(role="user", content="write an explicit one"),
    )
    return LLMRequest(messages=messages, tools=TOOLS, task="chat")


def drain(router: ModelRouter, request: LLMRequest) -> tuple[str, LLMResponse | None]:
    deltas: list[str] = []
    final: LLMResponse | None = None
    for item in router.chat_stream(request):
        if isinstance(item, LLMResponse):
            final = item
        else:
            deltas.append(item)
    return "".join(deltas), final


class RefusalDetectionTests(unittest.TestCase):

    def test_catches_refusals(self) -> None:
        for text in (
            "I'm sorry, but I can't help with that request.",
            "I cannot assist with creating that kind of content.",
            "I can't generate explicit or sexually explicit content,",
            "I'm unable to assist with that request.",
            "I'm not comfortable continuing in that direction.",
            "I must decline this request.",
            "As an AI language model, I am not able to produce that.",
            "<think>refuse this</think>I'm sorry, but I can't help with that.",
        ):
            self.assertTrue(looks_like_refusal(text), text)

    def test_ignores_legitimate_answers(self) -> None:
        for text in (
            "I don't know the answer to that.",
            "I couldn't find that file on disk.",
            "I can't find anything matching that in the index.",
            "I can't access that path because it is outside the allowed folders.",
            "I can't reach Ollama at 127.0.0.1:11434.",
            "I'm not sure, but the log suggests a timeout.",
            "I'm designing the schema now.",
            "The capital of France is Paris.",
        ):
            self.assertFalse(looks_like_refusal(text), text)


class RerouteTests(unittest.TestCase):

    def test_reroutes_on_the_tool_carrying_chat_path(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, routes())
        response = router.chat(graph_request())
        self.assertEqual(response.content, ANSWER)
        self.assertEqual(client.models_used, [PRIMARY, TARGET])

    def test_retry_swaps_system_prompt_and_keeps_history(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, routes())
        router.chat(graph_request())
        retry = client.requests[-1]
        system = [message for message in retry.messages if message.role == "system"]
        rest = [message.content for message in retry.messages if message.role != "system"]
        self.assertEqual(len(system), 1)
        self.assertEqual(system[0].content, DEFAULT_REROUTE_SYSTEM)
        self.assertEqual(rest, ["write one", "a tame story", "write an explicit one"])
        self.assertEqual(retry.tools, ())

    def test_no_reroute_when_the_model_called_a_tool(self) -> None:
        client = FakeClient(tool_call=True)
        router = ModelRouter(client, routes())
        response = router.chat(graph_request())
        self.assertTrue(response.tool_calls)
        self.assertEqual(client.models_used, [PRIMARY])

    def test_no_reroute_for_other_tasks_or_structured_output(self) -> None:
        for request in (
            LLMRequest.from_prompts("s", "u", task="intent"),
            LLMRequest.from_prompts("s", "u", task="chat", format="json"),
        ):
            client = FakeClient()
            router = ModelRouter(client, routes())
            router.chat(request)
            self.assertEqual(client.models_used, [PRIMARY])

    def test_disabled_when_unconfigured(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, routes(on_refusal=""))
        response = router.chat(graph_request())
        self.assertEqual(response.content, REFUSAL)
        self.assertEqual(client.models_used, [PRIMARY])


class RerouteStreamTests(unittest.TestCase):

    def test_refusal_never_reaches_the_caller(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, routes())
        text, final = drain(router, graph_request())
        self.assertEqual(text, ANSWER)
        self.assertNotIn("generate explicit", text)
        self.assertIsNotNone(final)
        self.assertEqual(final.content, ANSWER)
        self.assertEqual(client.models_used, [PRIMARY, TARGET])

    def test_clean_answer_passes_through_whole(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, ModelRoutes(default=TARGET, on_refusal="unused:1b"))
        text, _ = drain(router, LLMRequest.from_prompts("s", "u", task="chat"))
        self.assertEqual(text, ANSWER)
        self.assertEqual(client.models_used, [TARGET])

    def test_no_reroute_when_the_model_called_a_tool(self) -> None:
        client = FakeClient(tool_call=True)
        router = ModelRouter(client, routes())
        drain(router, graph_request())
        self.assertEqual(client.models_used, [PRIMARY])


class ForcedModelTests(unittest.TestCase):

    def test_forced_model_skips_the_primary(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, routes())
        router.force_model = TARGET
        response = router.chat(graph_request())
        self.assertEqual(response.content, ANSWER)
        self.assertEqual(client.models_used, [TARGET])

    def test_forced_model_applies_the_reroute_shape(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, routes())
        router.force_model = TARGET
        router.chat(graph_request())
        sent = client.requests[-1]
        system = [message for message in sent.messages if message.role == "system"]
        self.assertEqual(system[0].content, DEFAULT_REROUTE_SYSTEM)
        self.assertEqual(sent.tools, ())

    def test_forced_model_streams_from_the_target(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, routes())
        router.force_model = TARGET
        text, _ = drain(router, graph_request())
        self.assertEqual(text, ANSWER)
        self.assertEqual(client.models_used, [TARGET])

    def test_forced_model_leaves_other_tasks_alone(self) -> None:
        client = FakeClient()
        router = ModelRouter(client, routes())
        router.force_model = TARGET
        router.chat(LLMRequest.from_prompts("s", "u", task="intent"))
        self.assertEqual(client.models_used, [PRIMARY])


class UncensoredCommandTests(unittest.TestCase):

    def setUp(self) -> None:
        self.lines: list[str] = []
        self.router = ModelRouter(FakeClient(), routes())

    def _handler(self) -> UncensoredCommandHandler:
        return UncensoredCommandHandler(self.router, output=lambda text, role: self.lines.append(text))

    def test_ignores_other_commands(self) -> None:
        self.assertFalse(self._handler().handle("/models routes", {}))

    def test_on_then_off(self) -> None:
        handler = self._handler()
        self.assertTrue(handler.handle("/uncensored on", {}))
        self.assertEqual(self.router.force_model, TARGET)
        self.assertIn(TARGET, self.lines[-1])
        self.assertTrue(handler.handle("/uncensored off", {}))
        self.assertEqual(self.router.force_model, "")

    def test_status_reports_both_states(self) -> None:
        handler = self._handler()
        handler.handle("/uncensored", {})
        self.assertIn("is off", self.lines[-1].lower())
        handler.handle("/uncensored on", {})
        handler.handle("/uncensored", {})
        self.assertIn("is on", self.lines[-1].lower())

    def test_refuses_to_turn_on_without_a_configured_model(self) -> None:
        self.router = ModelRouter(FakeClient(), routes(on_refusal=""))
        handler = self._handler()
        handler.handle("/uncensored on", {})
        self.assertEqual(self.router.force_model, "")
        self.assertIn("no model", self.lines[-1].lower())


if __name__ == "__main__":
    unittest.main()
