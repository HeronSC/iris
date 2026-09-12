# File: core/tests/test_agent_graph.py

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.actions.implementations.add_document_root import AddDocumentRootAction
from core.actions.implementations.clipboard import ClipboardAction
from core.actions.implementations.launch_application import LaunchApplicationAction
from core.actions.implementations.open_file import OpenFileAction
from core.actions.implementations.open_folder import OpenFolderAction, ShowInExplorerAction
from core.actions.implementations.open_url import OpenUrlAction
from core.actions.implementations.scan_document_root import ScanDocumentRootAction
from core.actions.implementations.update_config import UpdateConfigAction
from core.actions.implementations.update_profile import UpdateProfileAction
from core.actions.models import ActionResult, ConfirmationPreview
from core.actions.registry import ActionRegistry
from core.agent.graph import IrisAgent
from core.llm.models import LLMRequest, LLMResponse, ToolCall
from core.tools.models import ToolDefinition, ToolKind


class FakeLLM:
    def __init__(self, response: str = '{"intent":"none","arguments":{}}') -> None:
        self.response = response
        self.requests: list[LLMRequest] = []
        self.answers: list[str] = ["Just chatting."]

    def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not request.tools:
            return LLMResponse(content=self.answers[-1])
        payload = json.loads(self.response)
        intent = str(payload.get("intent", "none"))
        if intent == "none":
            return LLMResponse(content=self.answers[-1])
        self.response = '{"intent":"none","arguments":{}}'
        return LLMResponse(content="", tool_calls=(ToolCall(name=intent, arguments=dict(payload.get("arguments", {}))),))

    def generate(self, system_prompt: str, user_prompt: str, task: str | None = None) -> str:
        return self.answers[-1]


class FakeExecutor:
    def __init__(self, registry: ActionRegistry, *, confirm: bool = False) -> None:
        self.registry = registry
        self.confirm = confirm
        self.requests: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None
        self.confirmed = 0
        self.cancelled = 0

    def execute(self, request: Any) -> ActionResult:
        if self.pending is not None:
            return ActionResult(status="rejected", message="Another action is already awaiting confirmation.", action=request.action, error="confirmation_already_pending")
        resolved = self.registry.resolve(request)
        if resolved is not None and resolved.error is None:
            request = resolved.request
        self.requests.append({"action": request.action, "arguments": request.arguments})
        if self.confirm:
            self.pending = {"action": request.action}
            return ActionResult(status="pending_confirmation", message=f"Awaiting confirmation for {request.action}", action=request.action)
        return ActionResult(status="success", message=f"Ran {request.action}", action=request.action)

    def has_pending_confirmation(self) -> bool:
        return self.pending is not None

    def pending_confirmation_preview(self) -> ConfirmationPreview | None:
        return ConfirmationPreview(summary=f"Run {self.pending['action']}") if self.pending else None

    def pending_description(self) -> str | None:
        return f"Run {self.pending['action']}" if self.pending else None

    def confirm_pending(self) -> ActionResult:
        self.confirmed += 1
        action = self.pending["action"] if self.pending else "?"
        self.pending = None
        return ActionResult(status="success", message=f"Confirmed {action}", action=action)

    def cancel_pending(self) -> ActionResult:
        self.cancelled += 1
        self.pending = None
        return ActionResult(status="cancelled", message="Pending action was cancelled.", action="cancel")


class FakeCoordinator:
    def __init__(self, llm: FakeLLM) -> None:
        self.ollama_client = llm
        self.completed: list[dict[str, Any]] = []
        self.file_ops: list[str] = []

    def parse_request(self, text: str) -> dict[str, Any]:
        if text.lower().startswith("find files"):
            return {"intent": "find_files", "requires_tool": True, "response_allowed": False}
        return {"intent": "respond", "requires_tool": False, "response_allowed": True}

    def is_help_request(self, text: str) -> bool:
        return text.strip() == "/help"

    def handle_file_operation(self, text: str, parser: dict[str, Any], project_id: str | None) -> str:
        self.file_ops.append(text)
        return "Found 3 files."

    def prepare_turn(self, text: str, project_id: str | None, *, for_tools: bool = False) -> dict[str, Any]:
        return {"user_message": text, "user_prompt": text, "system_prompt": "You are Iris."}

    def complete_turn(self, prepared: dict[str, Any], text: str, **kwargs: Any) -> str:
        self.completed.append({"text": text, **kwargs})
        return text

    def code_guidance(self) -> str:
        return "Answer with code."


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    for action in (OpenFileAction(), OpenFolderAction(), ShowInExplorerAction(), LaunchApplicationAction(), OpenUrlAction(), ClipboardAction(), AddDocumentRootAction(), ScanDocumentRootAction(), UpdateConfigAction(), UpdateProfileAction()):
        registry.register(action)
    return registry


class AgentRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()
        self.index_calls: list[dict[str, Any]] = []
        self.registry.tools.register(ToolDefinition(name="memory_review", description="Review memory", kind=ToolKind.COMMAND, handler=lambda args, state: self.index_calls.append({"memory_review": args}) or True))
        self.registry.tools.register(ToolDefinition(name="index_scan", description="Index", kind=ToolKind.COMMAND, handler=lambda args, state: self.index_calls.append({"index_scan": args}) or True))
        self.learned: list[tuple[str, str, dict[str, Any]]] = []

    def _agent(self, llm: FakeLLM, executor: FakeExecutor | None = None) -> tuple[IrisAgent, FakeCoordinator, FakeExecutor]:
        executor = executor or FakeExecutor(self.registry)
        coordinator = FakeCoordinator(llm)
        agent = IrisAgent(coordinator, tool_registry=self.registry.tools, action_executor=executor, on_tool_success=lambda text, name, args: self.learned.append((text, name, args)))
        return agent, coordinator, executor

    def test_tool_call_runs_through_the_executor_with_facets_flattened(self) -> None:
        agent, coordinator, executor = self._agent(FakeLLM('{"intent":"config_set_value","arguments":{"key":"model","value":"qwen3:8b"}}'))
        result = agent.run("set model to qwen3:8b", session_id="s1")
        self.assertEqual(executor.requests[-1]["action"], "update_config")
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "set_value")
        self.assertEqual(result.resolved_by, "tools")
        self.assertEqual(result.text, "Ran update_config")
        self.assertEqual(result.selected_tool, "config_set_value")
        self.assertEqual(self.learned[-1][1], "config_set_value")
        self.assertEqual(coordinator.completed[-1]["tool_status"]["status"], "success")

    def test_tool_events_report_start_and_end(self) -> None:
        events: list[dict] = []
        executor = FakeExecutor(self.registry)
        agent = IrisAgent(FakeCoordinator(FakeLLM('{"intent":"config_set_value","arguments":{"key":"model","value":"qwen3:8b"}}')), tool_registry=self.registry.tools, action_executor=executor, on_tool_event=events.append)
        agent.run("set model to qwen3:8b", session_id="events")
        self.assertEqual([item["phase"] for item in events], ["start", "end"])
        self.assertEqual(events[0]["name"], "config_set_value")
        self.assertEqual(events[0]["arguments"]["key"], "model")
        self.assertEqual(events[1]["status"], "success")
        self.assertGreaterEqual(events[1]["ms"], 0.0)

    def test_launch_and_profile_and_remove_facets(self) -> None:
        for response, action, key in (
            ('{"intent":"launch_application","arguments":{"app_name":"visual studio code"}}', "launch_application", "app_name"),
            ('{"intent":"profile_update","arguments":{"updates":{"display_name":"Henry"}}}', "update_profile", "updates"),
            ('{"intent":"config_remove_web_shortcut","arguments":{"name":"github"}}', "update_config", "name"),
            ('{"intent":"config_remove_application","arguments":{"app_id":"vscode"}}', "update_config", "app_id"),
        ):
            with self.subTest(action=action):
                agent, _, executor = self._agent(FakeLLM(response))
                agent.run("do it", session_id="s")
                self.assertEqual(executor.requests[-1]["action"], action)
                self.assertIn(key, executor.requests[-1]["arguments"])

    def test_command_tools_call_their_handlers(self) -> None:
        agent, _, executor = self._agent(FakeLLM('{"intent":"memory_review","arguments":{}}'))
        result = agent.run("could you go through what you remember", session_id="s2")
        self.assertEqual(self.index_calls, [{"memory_review": {}}])
        self.assertEqual(executor.requests, [])
        self.assertEqual(result.resolved_by, "tools")

    def test_plain_conversation_answers_without_tools(self) -> None:
        llm = FakeLLM()
        agent, coordinator, executor = self._agent(llm)
        result = agent.run("what is a good name for a cat?", session_id="s3")
        self.assertEqual(result.text, "Just chatting.")
        self.assertEqual(result.resolved_by, "model")
        self.assertEqual(executor.requests, [])
        self.assertTrue(llm.requests[-1].tools, "the answer call carries the tools so one model call decides")
        self.assertEqual(llm.requests[-1].task, "chat")

    def test_small_talk_and_code_skip_tools(self) -> None:
        llm = FakeLLM('{"intent":"launch_application","arguments":{"app_name":"code"}}')
        agent, coordinator, executor = self._agent(llm)
        agent.run("good morning", session_id="s4")
        self.assertEqual(executor.requests, [])
        self.assertFalse(llm.requests[-1].tools)
        agent.run("write a python function that reverses a string", session_id="s4")
        self.assertEqual(executor.requests, [])
        self.assertEqual(llm.requests[-1].task, "code")
        self.assertIn("Answer with code.", llm.requests[-1].messages[0].content)

    def test_file_operations_bypass_the_model(self) -> None:
        llm = FakeLLM()
        agent, coordinator, _ = self._agent(llm)
        result = agent.run("find files named roadmap", session_id="s5")
        self.assertEqual(result.text, "Found 3 files.")
        self.assertEqual(result.resolved_by, "file_ops")
        self.assertEqual(llm.requests, [])

    def test_unknown_tool_is_reported(self) -> None:
        agent, _, _ = self._agent(FakeLLM('{"intent":"teleport","arguments":{}}'))
        result = agent.run("teleport me", session_id="s6")
        self.assertIn("do not have a tool named teleport", result.text)


class AgentConfirmationTests(unittest.TestCase):
    def test_confirmation_pauses_then_resumes(self) -> None:
        registry = _registry()
        executor = FakeExecutor(registry, confirm=True)
        llm = FakeLLM('{"intent":"config_set_value","arguments":{"key":"model","value":"qwen3:8b"}}')
        agent = IrisAgent(FakeCoordinator(llm), tool_registry=registry.tools, action_executor=executor)

        first = agent.run("set model to qwen3:8b", session_id="conf")
        self.assertTrue(first.awaiting_confirmation)
        self.assertEqual(first.awaiting["summary"], "Run update_config")
        self.assertIn("Awaiting confirmation", first.text)
        self.assertIsNotNone(agent.awaiting("conf"))
        self.assertIsNone(agent.awaiting("other-session"))

        self.assertEqual(first.resolved_by, "confirmation")
        resumed = agent.resume("conf", True)
        self.assertIsNotNone(resumed)
        self.assertFalse(resumed.awaiting_confirmation)
        self.assertEqual(resumed.text, "Confirmed update_config")
        self.assertEqual(executor.confirmed, 1)
        self.assertEqual(len(executor.requests), 1, "the action is not executed a second time on resume")
        follow_up = agent.run("what is a good name for a cat?", session_id="conf")
        self.assertEqual(follow_up.resolved_by, "model", "nothing from the confirmed turn leaks into the next")
        self.assertIsNone(follow_up.selected_tool)
        self.assertIsNone(agent.awaiting("conf"))
        self.assertIsNone(agent.resume("conf", True), "nothing left to resume")

    def test_declining_cancels(self) -> None:
        registry = _registry()
        executor = FakeExecutor(registry, confirm=True)
        agent = IrisAgent(FakeCoordinator(FakeLLM('{"intent":"launch_application","arguments":{"app_name":"code"}}')), tool_registry=registry.tools, action_executor=executor)
        agent.run("launch visual studio", session_id="c2")
        declined = agent.resume("c2", False)
        self.assertEqual(executor.cancelled, 1)
        self.assertIn("cancelled", declined.text)

    def test_checkpoints_persist_across_agent_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _registry()
            executor = FakeExecutor(registry, confirm=True)
            path = Path(tmp) / "ckpt.db"
            agent = IrisAgent(FakeCoordinator(FakeLLM('{"intent":"launch_application","arguments":{"app_name":"code"}}')), tool_registry=registry.tools, action_executor=executor, checkpoint_path=path)
            agent.run("launch visual studio", session_id="persist")
            agent.close()
            again = IrisAgent(FakeCoordinator(FakeLLM()), tool_registry=registry.tools, action_executor=executor, checkpoint_path=path)
            self.assertIsNotNone(again.awaiting("persist"))
            resumed = again.resume("persist", True)
            self.assertEqual(resumed.text, "Confirmed launch_application")
            again.close()


class AgentStreamingTests(unittest.TestCase):
    def test_deltas_are_forwarded_and_cancel_stops_the_stream(self) -> None:
        import threading

        class StreamingLLM(FakeLLM):
            def __init__(self) -> None:
                super().__init__()
                self.closed = False

            def chat_stream(self, request: LLMRequest):
                try:
                    for piece in ("one ", "two ", "three"):
                        yield piece
                    yield LLMResponse(content="one two three")
                finally:
                    self.closed = True

        llm = StreamingLLM()
        agent = IrisAgent(FakeCoordinator(llm), tool_registry=_registry().tools, action_executor=FakeExecutor(_registry()))
        deltas: list[str] = []
        result = agent.run("tell me a story", session_id="st", on_delta=deltas.append)
        self.assertEqual(deltas, ["one ", "two ", "three"])
        self.assertEqual(result.text, "one two three")
        self.assertTrue(llm.closed)

        cancel = threading.Event()
        got: list[str] = []

        def listener(piece: str) -> None:
            got.append(piece)
            if len(got) == 2:
                cancel.set()

        llm2 = StreamingLLM()
        agent2 = IrisAgent(FakeCoordinator(llm2), tool_registry=_registry().tools, action_executor=FakeExecutor(_registry()))
        result2 = agent2.run("tell me a story", session_id="st2", on_delta=listener, cancel_event=cancel)
        self.assertEqual(got, ["one ", "two "])
        self.assertEqual(result2.text, "one two")


if __name__ == "__main__":
    unittest.main()
