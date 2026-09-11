# File: core/tests/test_routing_fixes.py

from __future__ import annotations

import tempfile
import unittest

from core.assistant.coordinator import AssistantCoordinator
from core.conversation.request_pipeline import RequestPipeline
from core.conversation.session_manager import SessionManager
from core.conversation.session_repository import SessionRepository
from core.profile.store import MemoryStore


class FileSearchGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline = RequestPipeline(config={})

    def _intent(self, text: str) -> str:
        return self.pipeline.build_request(text, state={}).intent

    def test_prose_with_a_search_verb_is_not_a_file_search(self) -> None:
        self.assertEqual(self._intent("I need a small Business central function that will lookup to to the customer List filters on customer ABC"), "respond")
        self.assertEqual(self._intent("lookup the vendor ledger entries for this customer"), "respond")
        self.assertEqual(self._intent("show me how to write a regex in python"), "respond")
        self.assertEqual(self._intent("list the steps to configure a service"), "respond")

    def test_real_file_requests_still_route_to_search(self) -> None:
        for text in (
            "find the invoice.pdf",
            "list any files with Ext in the name",
            "show files named roadmap",
            "display any files called release-notes",
            "can you look at indexed files for any file with bedroom or spare in the name?",
            "find documents about the pump filter",
        ):
            with self.subTest(text=text):
                request = self.pipeline.build_request(text, state={})
                self.assertEqual(request.intent, "find_files", text)
                self.assertNotIn(request.target.get("query"), {"to", "the", "a"})


class RecordingClient:
    def __init__(self) -> None:
        self.tasks: list[str | None] = []
        self.system_prompts: list[str] = []

    def generate(self, system_prompt: str, user_prompt: str, task: str | None = None) -> str:
        self.tasks.append(task)
        self.system_prompts.append(system_prompt)
        if task == "decision":
            return '{"decision": "respond"}'
        return "answer"


class CodeTaskRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        manager = SessionManager(SessionRepository(self._tmp.name))
        manager.start_session(title="t")
        store = MemoryStore({"profile": {"profile": {}}, "preferences": {"preferences": []}, "projects": {"projects": []}, "knowledge": {"knowledge_areas": []}})
        self.client = RecordingClient()
        self.coordinator = AssistantCoordinator("Iris", store, {}, self.client, session_manager=manager)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_code_requests_go_to_the_code_task(self) -> None:
        self.coordinator.respond_detailed("I need a small Business Central function that filters the customer list on customer ABC")
        self.assertEqual(self.client.tasks, ["code"], "no decision call, straight to the code model")
        self.assertIn("means AL", self.client.system_prompts[-1])

    def test_plain_questions_stay_on_chat(self) -> None:
        self.coordinator.respond_detailed("what is a good name for a cat?")
        self.assertIn("chat", self.client.tasks)
        self.assertNotIn("code", self.client.tasks)
        self.assertNotIn("means AL", self.client.system_prompts[-1])


if __name__ == "__main__":
    unittest.main()
