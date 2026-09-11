# File: core/tests/test_streaming.py

from __future__ import annotations

import tempfile
import threading
import unittest
from collections.abc import Iterator

from core.application import IrisApplication, IrisEvent, IrisStatus
from core.assistant.coordinator import AssistantCoordinator, CoordinatorTurn
from core.conversation.session_manager import SessionManager
from core.conversation.session_repository import SessionRepository
from core.llm.models import LLMRequest, LLMResponse, LLMUsage
from core.profile.store import MemoryStore


class StreamingClient:
    """Answers the orchestrator with 'respond' and streams the reply in pieces."""

    def __init__(self, pieces: list[str], *, stop_after: int | None = None) -> None:
        self.pieces = pieces
        self.stop_after = stop_after
        self.generate_calls: list[str | None] = []
        self.stream_requests: list[LLMRequest] = []
        self.closed = False

    def generate(self, system_prompt: str, user_prompt: str, task: str | None = None) -> str:
        self.generate_calls.append(task)
        if task == "decision":
            return '{"decision": "respond"}'
        return "non-streamed answer"

    def chat(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="".join(self.pieces))

    def chat_stream(self, request: LLMRequest) -> Iterator[str | LLMResponse]:
        self.stream_requests.append(request)
        try:
            for piece in self.pieces:
                yield piece
            yield LLMResponse(content="".join(self.pieces), model="qwen3:8b", usage=LLMUsage(prompt_tokens=5, completion_tokens=len(self.pieces)))
        finally:
            self.closed = True


def _store() -> MemoryStore:
    return MemoryStore({"profile": {"profile": {}}, "preferences": {"preferences": []}, "projects": {"projects": []}, "knowledge": {"knowledge_areas": []}})


class CoordinatorStreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.manager = SessionManager(SessionRepository(self._tmp.name))
        self.manager.start_session(title="Stream")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _coordinator(self, client: StreamingClient) -> AssistantCoordinator:
        return AssistantCoordinator("Iris", _store(), {}, client, session_manager=self.manager)

    def test_deltas_arrive_in_order_and_the_answer_is_the_whole_text(self) -> None:
        client = StreamingClient(["The ", "pump ", "filter."])
        coordinator = self._coordinator(client)
        deltas: list[str] = []

        turn = coordinator.respond_detailed("what about the pump?", on_delta=deltas.append)

        self.assertEqual(deltas, ["The ", "pump ", "filter."])
        self.assertEqual(turn.text, "The pump filter.")
        self.assertEqual(client.stream_requests[-1].task, "chat")
        self.assertNotIn("chat", client.generate_calls, "the streamed path replaces the blocking one")
        self.assertTrue(client.closed)

    def test_without_a_listener_the_blocking_path_is_used(self) -> None:
        client = StreamingClient(["unused"])
        coordinator = self._coordinator(client)
        turn = coordinator.respond_detailed("hello")
        self.assertEqual(turn.text, "non-streamed answer")
        self.assertEqual(client.stream_requests, [])

    def test_cancel_stops_the_stream_and_keeps_what_arrived(self) -> None:
        client = StreamingClient(["one ", "two ", "three"])
        coordinator = self._coordinator(client)
        cancel = threading.Event()
        deltas: list[str] = []

        def listener(piece: str) -> None:
            deltas.append(piece)
            if len(deltas) == 2:
                cancel.set()

        turn = coordinator.respond_detailed("count", on_delta=listener, cancel_event=cancel)
        self.assertEqual(deltas, ["one ", "two "])
        self.assertEqual(turn.text, "one two")
        self.assertTrue(client.closed, "closing the generator ends the HTTP stream")


class _StreamingCoordinatorStub:
    def __init__(self) -> None:
        self.received_on_delta: list[bool] = []

    def respond_detailed(self, *_args, on_delta=None, cancel_event=None, **_kwargs) -> CoordinatorTurn:
        self.received_on_delta.append(on_delta is not None)
        if on_delta is not None:
            on_delta("Hel")
            on_delta("lo.")
        return CoordinatorTurn(text="Hello.")

    def summarize_for_chat(self, **_kwargs) -> str:
        return "Hello."

    def suggest_follow_up_actions(self, **_kwargs) -> list:
        return []


class _False:
    def handle(self, *_args, **_kwargs) -> bool:
        return False

    def handle_natural_language(self, *_args, **_kwargs) -> bool:
        return False

    def has_pending_confirmation(self) -> bool:
        return False

    def consume_expired_confirmation_notice(self) -> bool:
        return False

    def handle_pending(self, *_args, **_kwargs) -> bool:
        return False


class ServiceStreamingTests(unittest.TestCase):
    def _app(self) -> tuple[IrisApplication, _StreamingCoordinatorStub]:
        app = IrisApplication()
        app.initialized = True
        app.state = {"assistant_name": "Iris"}
        stub = _StreamingCoordinatorStub()
        for name in ("action_executor", "pending_action_manager", "save_handler", "session_handler", "proposal_handler", "memory_handler", "search_handler", "action_handler", "intent_router", "project_handler", "index_handler"):
            setattr(app, name, _False())
        app.coordinator = stub
        return app, stub

    def test_deltas_become_events_when_a_handler_listens(self) -> None:
        app, stub = self._app()
        events: list[IrisEvent] = []
        response = app.process_message("hi there", event_handler=events.append)
        self.assertEqual([event.delta for event in events if event.delta], ["Hel", "lo."])
        self.assertTrue(all(event.status == IrisStatus.THINKING for event in events if event.delta))
        self.assertEqual(stub.received_on_delta, [True])
        self.assertEqual(response.conversation.message, "Hello.")

    def test_no_handler_means_no_streaming(self) -> None:
        app, stub = self._app()
        app.process_message("hi there")
        self.assertEqual(stub.received_on_delta, [False])


if __name__ == "__main__":
    unittest.main()
