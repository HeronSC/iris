import unittest
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from core.actions.models import ActionResult
from core.assistant.conversation_synonyms import ConversationSynonymStore
from core.assistant.intent_example_store import IntentExampleStore
from core.assistant.intent_router import IntentRouter


class FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return self.response


class FakeHandler:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        self.commands.append(user_input)
        return True


class FakeExecutor:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.context = type(
            "Context",
            (),
            {
                "allowed_roots": [],
                "web_shortcuts": {"bc-sandbox": "https://example.test/sandbox"},
            },
        )()

    def execute(self, request: Any) -> ActionResult:
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
