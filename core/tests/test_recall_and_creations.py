# File: core/tests/test_recall_and_creations.py

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.recall_tools import RecallConversationAction
from core.actions.models import ActionRequest
from core.assistant.keep_command import KeepCommandHandler
from core.conversation.creations import CreationStore, derive_title, detect_kind
from core.conversation.session_manager import SessionManager
from core.conversation.session_repository import SessionRepository

STORY = "Joe walked along the sandy shore, the salty ocean breeze tousling his dark hair. " * 8


class DetectionTests(unittest.TestCase):
    def test_creative_requests_are_detected(self) -> None:
        cases = {
            "create a sexually explicit story with 500 words that tells about joe and julie meeting on the beach": "story",
            "write me a letter to my landlord about the broken heater": "letter",
            "continue the story": "story",
            "give me a recipe for banana bread": "recipe",
            "draft an email to the team about Friday": "email",
            "tell me a joke about cats": "joke",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(detect_kind(message, STORY), expected)

    def test_non_creative_or_short_or_refused_are_skipped(self) -> None:
        self.assertIsNone(detect_kind("good morning", STORY))
        self.assertIsNone(detect_kind("write a function that parses json", STORY))
        self.assertIsNone(detect_kind("create a story about a beach", "Short."))
        self.assertIsNone(detect_kind("create a story about a beach", "I am unable to assist with that request. " * 20))
        self.assertIsNone(detect_kind("/keep", STORY))

    def test_title_comes_from_heading_then_prompt(self) -> None:
        self.assertEqual(derive_title("create a story about joe", '**"Sunset Serendipity"**\n\nThe sun dipped low', "story"), "Sunset Serendipity")
        self.assertEqual(derive_title("create a story about joe and julie meeting on the beach", STORY, "story"), "Joe and julie meeting on the beach")
        self.assertTrue(derive_title("write a story", STORY, "story").startswith("Story "))


class CreationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "Creations"
        self.indexed: list[Path] = []
        self.store = CreationStore(self.folder, indexer=self.indexed.append)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_capture_saves_indexes_and_dedupes_names(self) -> None:
        first = self.store.capture(user_message="write a story about a couple on a beach", response=STORY, session_id="s1")
        second = self.store.capture(user_message="write a story about a couple on a beach", response=STORY, session_id="s1")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.path, second.path)
        self.assertTrue(first.path.name.endswith("A couple on a beach.md"))
        self.assertEqual(self.indexed, [first.path, second.path])
        body = first.path.read_text(encoding="utf-8")
        self.assertIn("# A couple on a beach", body)
        self.assertIn("Kind: story", body)
        self.assertIn("Session: s1", body)
        self.assertIn("Joe walked along", body)

    def test_capture_skips_when_a_file_tool_already_wrote(self) -> None:
        self.assertIsNone(self.store.capture(user_message="write a story about a beach", response=STORY, session_id="s1", selected_tool="write_file"))
        self.assertFalse(self.folder.exists())

    def test_search_matches_all_words_newest_first(self) -> None:
        self.store.save(title="Beach story", content=STORY, kind="story", prompt="p", session_id="s1")
        self.store.save(title="Landlord letter", content="Dear landlord, the heater is broken.", kind="letter", prompt="p", session_id="s1")
        hits = self.store.search(["heater", "broken"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["title"], "Landlord letter")
        self.assertEqual(len(self.store.search([])), 2)


class RecallToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repository = SessionRepository(root / "sessions")
        manager = SessionManager(self.repository)
        manager.start_session(title="Beach story")
        manager.add_message("user", "create a story about joe and julie on the beach")
        manager.add_message("assistant", STORY)
        manager.save_active_session()
        manager.close_active_session()
        manager.start_session(title="Heater")
        manager.add_message("user", "what do I tell the landlord about the heater")
        manager.add_message("assistant", "Tell the landlord the heater has been broken since Monday.")
        manager.save_active_session()
        self.creations = CreationStore(root / "Creations")
        self.context = SimpleNamespace(session_repository=self.repository, creations=self.creations)
        self.action = RecallConversationAction()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, **arguments: object):
        request = ActionRequest(action="recall_conversation", arguments=dict(arguments))
        validation = self.action.validate(request, self.context)
        self.assertTrue(validation.ok, validation.error)
        return self.action.execute(ActionRequest(action="recall_conversation", arguments=validation.resolved_arguments), self.context)

    def test_finds_full_text_of_past_assistant_message(self) -> None:
        result = self._run(query="joe sandy shore", role="assistant")
        self.assertEqual(result.status, "success")
        self.assertIn("1 match", result.message)
        self.assertIn("Beach story", result.message)
        self.assertIn(STORY.strip(), result.message)
        self.assertEqual(len(result.results), 1)

    def test_includes_saved_documents(self) -> None:
        self.creations.save(title="Sunset Serendipity", content=STORY, kind="story", prompt="p", session_id="x")
        result = self._run(query="sandy shore")
        self.assertIn("Saved document:", result.message)
        self.assertIn("Sunset Serendipity", result.message)
        self.assertEqual(len(result.results), 2)

    def test_reports_nothing_found_and_rejects_bad_role(self) -> None:
        result = self._run(query="submarine")
        self.assertEqual(result.status, "success")
        self.assertIn("Nothing in the last 30 days", result.message)
        bad = self.action.validate(ActionRequest(action="recall_conversation", arguments={"query": "x", "role": "robot"}), self.context)
        self.assertFalse(bad.ok)

    def test_snippets_when_full_text_is_off(self) -> None:
        result = self._run(query="heater", full_text=False)
        self.assertIn("Tell the landlord", result.message)
        self.assertNotIn(STORY.strip(), result.message)

    def test_unavailable_without_repository(self) -> None:
        result = self.action.execute(ActionRequest(action="recall_conversation", arguments={"query": "x"}), SimpleNamespace())
        self.assertEqual(result.status, "failed")


class KeepCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.manager = SessionManager(SessionRepository(root / "sessions"))
        self.store = CreationStore(root / "Creations")
        self.lines: list[str] = []
        self.handler = KeepCommandHandler(self.store, self.manager, output=lambda text, role=None: self.lines.append(text))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_keep_saves_last_answer_with_given_or_derived_title(self) -> None:
        self.manager.start_session()
        self.assertTrue(self.handler.handle("/keep", {}))
        self.assertIn("Nothing to keep", self.lines[-1])
        self.manager.add_message("user", "write a poem about rain")
        self.manager.add_message("assistant", "Rain falls softly on the roof. " * 20)
        self.assertTrue(self.handler.handle("/keep Rain poem", {}))
        self.assertIn("Kept as", self.lines[-1])
        saved = list(self.store.folder.glob("*.md"))
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].name.endswith("Rain poem.md"))
        self.assertIn("Kind: poem", saved[0].read_text(encoding="utf-8"))
        self.assertFalse(self.handler.handle("/save", {}))


if __name__ == "__main__":
    unittest.main()
