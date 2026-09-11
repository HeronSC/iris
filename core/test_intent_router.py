# File: core/test_intent_router.py

import unittest
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import json

from core.actions.implementations.add_document_root import AddDocumentRootAction
from core.actions.implementations.clipboard import ClipboardAction
from core.actions.implementations.launch_application import LaunchApplicationAction
from core.actions.implementations.open_file import OpenFileAction
from core.actions.implementations.open_folder import OpenFolderAction, ShowInExplorerAction
from core.actions.implementations.open_url import OpenUrlAction
from core.actions.implementations.scan_document_root import ScanDocumentRootAction
from core.actions.implementations.update_config import UpdateConfigAction
from core.actions.implementations.update_profile import UpdateProfileAction
from core.actions.models import ActionResult
from core.actions.registry import ActionRegistry
from core.llm.models import LLMRequest, LLMResponse, ToolCall
from core.assistant.conversation_synonyms import ConversationSynonymStore
from core.assistant.intent_example_store import IntentExampleStore
from core.assistant.intent_router import IntentRouter


class FakeLLM:
    """Answers every chat with the tool call encoded in ``response``.

    ``response`` keeps the old ``{"intent": ..., "arguments": {...}}`` JSON so
    the test cases read the same; intent ``none`` becomes a plain text reply.
    """

    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[LLMRequest] = []

    def generate(self, system_prompt: str, user_prompt: str, task: str | None = None) -> str:
        return self.response

    def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        payload = json.loads(self.response)
        intent = str(payload.get("intent", "none"))
        if intent == "none":
            return LLMResponse(content="Just chatting.")
        return LLMResponse(content="", tool_calls=(ToolCall(name=intent, arguments=dict(payload.get("arguments", {}))),))


def build_action_registry() -> ActionRegistry:
    registry = ActionRegistry()
    for action in (
        OpenFileAction(),
        OpenFolderAction(),
        ShowInExplorerAction(),
        LaunchApplicationAction(),
        OpenUrlAction(),
        ClipboardAction(),
        AddDocumentRootAction(),
        ScanDocumentRootAction(),
        UpdateConfigAction(),
        UpdateProfileAction(),
    ):
        registry.register(action)
    return registry


class FakeHandler:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        self.commands.append(user_input)
        return True


class FakeExecutor:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.registry = build_action_registry()
        self.context = type(
            "Context",
            (),
            {
                "allowed_roots": [],
                "web_shortcuts": {"bc-sandbox": "https://example.test/sandbox"},
            },
        )()

    def execute(self, request: Any) -> ActionResult:
        # Mirror the real executor: a facet such as config_set_value flattens onto
        # its underlying action with the bound arguments merged in.
        resolved = self.registry.resolve(request)
        if resolved is not None and resolved.error is None:
            request = resolved.request
        self.requests.append({"action": request.action, "arguments": request.arguments, "follow_up": request.follow_up})
        return ActionResult(status="pending_confirmation", message="Action requires confirmation. Run /confirm to continue.", action=request.action)


class IntentRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.example_store = IntentExampleStore(Path(self.tempdir.name) / "intent_examples.json")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_routes_index_scan_intent(self) -> None:
        llm = FakeLLM('{"intent":"index_scan","arguments":{"root":"documents"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("please index my documents folder", {})

        self.assertTrue(handled)
        self.assertEqual(index.commands[-1], "/index scan documents")
        self.assertEqual(memory.commands, [])
        self.assertEqual(executor.requests, [])

    def test_routes_memory_scan_intent(self) -> None:
        llm = FakeLLM('{"intent":"memory_scan","arguments":{}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("scan memory updates", {})

        self.assertTrue(handled)
        self.assertEqual(memory.commands[-1], "/memory scan")

    def test_routes_memory_review_from_remember_phrase(self) -> None:
        llm = FakeLLM('{"intent":"memory_review","arguments":{}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("could you go through what you remember", {})

        self.assertTrue(handled)
        self.assertEqual(memory.commands[-1], "/memory review")

    def test_routes_index_path_directly_without_llm_classification(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_document_root","arguments":{"root":"D:\\\\Wrong"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("can you index D:\\HenryZuraw\\Documents", {})

        self.assertTrue(handled)
        self.assertEqual(index.commands, [])
        self.assertEqual(executor.requests[-1]["action"], "add_document_root")
        self.assertIsNotNone(executor.requests[-1]["follow_up"])
        self.assertEqual(executor.requests[-1]["follow_up"].action, "scan_document_root")

    def test_routes_config_add_application_to_action_executor(self) -> None:
        llm = FakeLLM(
            '{"intent":"config_add_application","arguments":{"app_id":"excel","display_name":"Excel","executable":"C:\\\\Program Files\\\\Microsoft Office\\\\root\\\\Office16\\\\EXCEL.EXE","aliases":["excel"]}}'
        )
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("add application excel", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests[-1]["action"], "update_config")
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "add_application")

    def test_routes_config_set_value_to_action_executor(self) -> None:
        llm = FakeLLM('{"intent":"config_set_value","arguments":{"key":"model","value":"qwen3:8b"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("set model to qwen3:8b", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests[-1]["action"], "update_config")
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "set_value")
        self.assertEqual(executor.requests[-1]["arguments"]["key"], "model")

    def test_routes_profile_update_to_action_executor(self) -> None:
        llm = FakeLLM('{"intent":"profile_update","arguments":{"updates":{"display_name":"Henry"}}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("update my profile display name to Henry", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests[-1]["action"], "update_profile")
        self.assertEqual(executor.requests[-1]["arguments"]["updates"], {"display_name": "Henry"})

    def test_routes_common_language_stop_indexing_to_remove_root(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_document_root","arguments":{"root":"D:\\\\HenryZuraw\\\\Documents"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("would you please stop indexing this folder D:\\HenryZuraw\\Documents", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests[-1]["action"], "update_config")
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "remove_document_root")

    def test_ambiguous_stop_indexing_request_requires_clarification(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_document_root","arguments":{"root":"D:\\\\HenryZuraw\\\\Documents"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)

        with tempfile.TemporaryFile(mode="w+"):
            handled = router.handle("Stop indexing D:\\HenryZuraw\\Documents", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests, [])

        handled_follow_up = router.handle("2", {})

        self.assertTrue(handled_follow_up)
        self.assertEqual(executor.requests[-1]["action"], "update_config")
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "remove_document_root")

    def test_routes_no_longer_index_language_to_remove_root(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_document_root","arguments":{"root":"D:\\\\HenryZuraw\\\\Documents"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("hello, can you no longer index this folder D:\\HenryZuraw\\Documents", {})

        self.assertTrue(handled)
        self.assertEqual(index.commands, [])
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "remove_document_root")

    def test_routes_remove_application_to_action_executor(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_application","arguments":{"app_id":"vscode"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("please delete application vscode", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "remove_application")

    def test_routes_remove_web_shortcut_to_action_executor(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_web_shortcut","arguments":{"name":"github"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("remove web shortcut github", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "remove_web_shortcut")

    def test_non_actionable_text_returns_false(self) -> None:
        llm = FakeLLM('{"intent":"none","arguments":{}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        handled = router.handle("how are you today", {})

        self.assertFalse(handled)

    def test_correction_flow_saves_approved_mapping(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_document_root","arguments":{"root":"D:\\\\HenryZuraw\\\\Documents"}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, memory, executor, self.example_store)
        self.assertTrue(router.handle("please remove this folder from indexing", {}))
        self.assertTrue(router.handle("That is not what I meant", {}))

        llm.response = '{"intent":"launch_application","arguments":{"app_name":"vscode"}}'
        self.assertTrue(router.handle("launch visual studio code", {}))
        self.assertTrue(router.handle("okay", {}))

        stored = self.example_store.resolve("please remove this folder from indexing")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.intent, "launch_application")

    def test_invented_path_for_scan_falls_back_to_reindexing_configured_roots(self) -> None:
        llm = FakeLLM('{"intent":"scan_document_root","arguments":{"root":"E:\\\\no_think"}}')
        index = FakeHandler()
        executor = FakeExecutor()

        router = IntentRouter(llm, index, FakeHandler(), executor, self.example_store)
        with redirect_stdout(StringIO()):
            handled = router.handle("index my documents folder", {})

        self.assertTrue(handled)
        self.assertEqual(index.commands, ["/index scan"])
        self.assertEqual(executor.requests, [])

    def test_invented_path_for_config_change_asks_instead_of_acting(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_document_root","arguments":{"root":"D:\\\\Somewhere\\\\Else"}}')
        executor = FakeExecutor()

        router = IntentRouter(llm, FakeHandler(), FakeHandler(), executor, self.example_store)
        buffer = StringIO()
        with redirect_stdout(buffer):
            handled = router.handle("stop indexing that folder please", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests, [])
        self.assertIn("Which folder?", buffer.getvalue())

    def test_path_present_in_message_is_accepted(self) -> None:
        llm = FakeLLM('{"intent":"config_remove_document_root","arguments":{"root":"D:/HenryZuraw/Documents"}}')
        executor = FakeExecutor()

        router = IntentRouter(llm, FakeHandler(), FakeHandler(), executor, self.example_store)
        with redirect_stdout(StringIO()):
            handled = router.handle("can you no longer index this folder D:\\HenryZuraw\\Documents", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests[-1]["arguments"]["operation"], "remove_document_root")
        self.assertEqual(executor.requests[-1]["arguments"]["root"], "D:/HenryZuraw/Documents")

    def test_classifier_sends_registry_tools_and_examples(self) -> None:
        llm = FakeLLM('{"intent":"launch_application","arguments":{"app_name":"vscode"}}')
        self.example_store.save_example("fire up code", "launch_application", {"app_name": "vscode"}, source="test")
        router = IntentRouter(llm, FakeHandler(), FakeHandler(), FakeExecutor(), self.example_store)
        with redirect_stdout(StringIO()):
            router.handle("launch that editor for me", {})

        request = llm.requests[-1]
        self.assertEqual(request.think, False)
        names = {tool.name for tool in request.tools}
        self.assertIn("launch_application", names)
        self.assertIn("config_set_value", names)
        self.assertIn("memory_review", names)
        self.assertNotIn("update_config", names, "the underlying action is not offered to the model")
        self.assertIn("fire up code", request.messages[0].content)

    def test_phrase_alias_routes_without_llm(self) -> None:
        llm = FakeLLM('{"intent":"none","arguments":{}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()
        synonym_store = ConversationSynonymStore(Path(self.tempdir.name) / "Configuration" / "conversation_synonyms.json")
        synonym_store.store_path.write_text(
            '{\n'
            '  "confirm": ["yes"],\n'
            '  "cancel": ["cancel"],\n'
            '  "aliases": [\n'
            '    {\n'
            '      "phrase": "open sandbox",\n'
            '      "intent": "open_url_shortcut",\n'
            '      "arguments": {"shortcut": "bc-sandbox"}\n'
            '    }\n'
            '  ]\n'
            '}\n',
            encoding="utf-8",
        )

        router = IntentRouter(llm, index, memory, executor, self.example_store, conversation_synonyms=synonym_store)
        handled = router.handle("open sandbox", {})

        self.assertTrue(handled)
        self.assertEqual(executor.requests[-1]["action"], "open_url")
        self.assertEqual(executor.requests[-1]["arguments"]["shortcut"], "bc-sandbox")

    def test_can_teach_confirmation_phrases_without_llm(self) -> None:
        llm = FakeLLM('{"intent":"none","arguments":{}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()
        synonym_store = ConversationSynonymStore(Path(self.tempdir.name) / "Configuration" / "conversation_synonyms.json")

        router = IntentRouter(llm, index, memory, executor, self.example_store, conversation_synonyms=synonym_store)

        output = StringIO()
        with redirect_stdout(output):
            handled_prompt = router.handle("can I give you some words for confirm that I often use?", {})
            handled_store = router.handle(
                "I want them stored so I can use them. Confirm, affirmative, do it, go for it, sounds good, make it so, you got it, yup, yes please, engage",
                {},
            )

        self.assertTrue(handled_prompt)
        self.assertTrue(handled_store)
        self.assertEqual(executor.requests, [])
        self.assertIn("share the confirm phrases", output.getvalue().lower())
        self.assertIn("stored confirm phrases", output.getvalue().lower())
        self.assertEqual(synonym_store.resolve_pending_response("affirmative"), "confirm")
        self.assertEqual(synonym_store.resolve_pending_response("go for it"), "confirm")
        self.assertEqual(synonym_store.resolve_pending_response("engage"), "confirm")

    def test_can_store_confirmation_phrases_in_one_message(self) -> None:
        llm = FakeLLM('{"intent":"none","arguments":{}}')
        index = FakeHandler()
        memory = FakeHandler()
        executor = FakeExecutor()
        synonym_store = ConversationSynonymStore(Path(self.tempdir.name) / "Configuration" / "conversation_synonyms.json")

        router = IntentRouter(llm, index, memory, executor, self.example_store, conversation_synonyms=synonym_store)

        output = StringIO()
        with redirect_stdout(output):
            handled = router.handle(
                "I want them stored so I can use them. Confirm, affirmative, do it, go for it, sounds good, make it so, you got it, yup, yes please, engage",
                {},
            )

        self.assertTrue(handled)
        self.assertEqual(executor.requests, [])
        self.assertIn("stored confirm phrases", output.getvalue().lower())
        self.assertEqual(synonym_store.resolve_pending_response("affirmative"), "confirm")
        self.assertEqual(synonym_store.resolve_pending_response("you got it"), "confirm")


if __name__ == "__main__":
    unittest.main()


class ActionableGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.example_store = IntentExampleStore(Path(self.tempdir.name) / "intent_examples.json")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _router(self) -> IntentRouter:
        from core.tools.models import ToolDefinition, ToolKind

        executor = FakeExecutor()
        router = IntentRouter(FakeLLM('{"intent":"none","arguments":{}}'), FakeHandler(), FakeHandler(), executor, self.example_store)
        router.tool_registry.register(ToolDefinition(name="disk_usage", description="x", kind=ToolKind.COMMAND, keywords=("space", "storage"), handler=lambda a, s: True))
        router.tool_registry.register(ToolDefinition(name="windows_services", description="x", kind=ToolKind.COMMAND, keywords=("service",), handler=lambda a, s: True))
        return router

    def test_declared_keywords_and_name_words_open_the_gate(self) -> None:
        router = self._router()
        self.assertTrue(router._looks_actionable("what's eating space on E:?"))
        self.assertTrue(router._looks_actionable("is the ollama service running?"))
        self.assertTrue(router._looks_actionable("list windows services"))
        self.assertTrue(router._looks_actionable("how much storage is left"))

    def test_word_boundaries_and_disabled_tools(self) -> None:
        router = self._router()
        self.assertFalse(router._looks_actionable("tell me about spacex"))
        self.assertFalse(router._looks_actionable("what's a good name for a cat?"))
        router.tool_registry.disable("disk_usage")
        self.assertFalse(router._looks_actionable("how much storage is left"))
