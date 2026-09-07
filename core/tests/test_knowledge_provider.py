from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.assistant.general_knowledge_router import GeneralKnowledgeRouter, ProviderExecutionError
from core.assistant.knowledge_provider import KnowledgeRecallProvider, RecallRequest
from core.knowledge import (
    KnowledgeGraph,
    KnowledgeRetriever,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
)
from core.storage.sqlite_database import SQLiteDatabase

TOPIC = "trading/candidates"


class RecallProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        database = SQLiteDatabase(Path(self._tmp.name) / "knowledge.db")
        self.graph = KnowledgeGraph(database)
        self.provider = KnowledgeRecallProvider(KnowledgeRetriever(database))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, content: str, **overrides: object) -> MemoryRecord:
        values: dict[str, object] = {
            "kind": MemoryKind.OBSERVATION,
            "topic": TOPIC,
            "content": content,
            "source": "scanner",
        }
        values.update(overrides)
        return self.graph.records.add(MemoryRecord(**values))  # type: ignore[arg-type]

    # -- how the planner sees it --------------------------------------------

    def test_it_advertises_itself_as_a_capability(self) -> None:
        definition = self.provider.definition()

        self.assertEqual(definition.name, "recall")
        self.assertIn("query", definition.request_schema)
        self.assertIn("previously observed", definition.description)

    def test_there_is_no_keyword_route_to_it(self) -> None:
        """Reached through the planner, not by matching words in the message."""
        for text in ("what do we know about ABC", "recall", "remember when"):
            self.assertFalse(self.provider.can_handle(text))

    def test_a_request_needs_a_query(self) -> None:
        self.assertIsNone(self.provider.parse_request({}))
        self.assertIsNone(self.provider.parse_request({"query": "   "}))

    def test_a_request_takes_the_design_vocabulary(self) -> None:
        request = self.provider.parse_request(
            {"query": "relative volume", "topic": TOPIC, "kinds": ["observation", "outcome"], "limit": 3}
        )

        assert request is not None
        self.assertEqual(request.query, "relative volume")
        self.assertEqual(request.topic, TOPIC)
        self.assertEqual(request.kinds, (MemoryKind.OBSERVATION, MemoryKind.OUTCOME))
        self.assertEqual(request.limit, 3)

    def test_unknown_kinds_are_ignored_rather_than_fatal(self) -> None:
        request = self.provider.parse_request({"query": "x", "kinds": ["observation", "nonsense"]})

        assert request is not None
        self.assertEqual(request.kinds, (MemoryKind.OBSERVATION,))

    def test_the_limit_is_clamped(self) -> None:
        self.assertEqual(self.provider.parse_request({"query": "x", "limit": 900}).limit, 20)
        self.assertEqual(self.provider.parse_request({"query": "x", "limit": 0}).limit, 1)
        self.assertEqual(self.provider.parse_request({"query": "x", "limit": "nope"}).limit, 6)

    # -- what it returns ------------------------------------------------------

    def test_recall_returns_what_was_recorded(self) -> None:
        self._add("ABC showed relative volume acceleration of 2.1x")
        self._add("unrelated note about the printer", topic="home/office")

        result = self.provider.execute_detailed_request(
            RecallRequest(query="relative volume acceleration")
        )

        assert result is not None
        self.assertIn("ABC showed relative volume", result.response or "")
        self.assertNotIn("printer", result.response or "")
        self.assertEqual(result.metadata["found"], 1)
        self.assertEqual(result.detail_type, "markdown")

    def test_an_empty_store_says_so_rather_than_returning_nothing(self) -> None:
        """Silence reads to the model as 'there is nothing to know', a different claim."""
        result = self.provider.execute_detailed_request(RecallRequest(query="anything at all"))

        assert result is not None
        self.assertIn("Nothing recorded", result.response or "")
        self.assertEqual(result.metadata["found"], 0)

    def test_rejected_and_superseded_records_are_not_recalled(self) -> None:
        original = self._add("v1 belief")
        self.graph.records.supersede(original.id, MemoryRecord(
            kind=MemoryKind.OBSERVATION, topic=TOPIC, source="scanner", content="v2 belief"))
        self._add("a discarded idea", kind=MemoryKind.HYPOTHESIS, status=MemoryStatus.REJECTED, source="analysis")

        result = self.provider.execute_detailed_request(RecallRequest(query="belief idea"))

        assert result is not None
        text = result.response or ""
        self.assertIn("v2 belief", text)
        self.assertNotIn("v1 belief", text)
        self.assertNotIn("discarded", text)

    def test_kind_and_status_are_shown_so_a_guess_is_not_read_as_fact(self) -> None:
        self._add("Volume predicts outperformance.", kind=MemoryKind.HYPOTHESIS,
                  status=MemoryStatus.TESTING, source="analysis")

        result = self.provider.execute_detailed_request(RecallRequest(query="volume predicts"))

        assert result is not None
        self.assertIn("[hypothesis, testing]", result.response or "")

    def test_a_truncated_search_says_so_in_the_detail(self) -> None:
        for index in range(30):
            self._add(f"observation {index}")
        provider = KnowledgeRecallProvider(self.provider.retriever)

        result = provider.execute_detailed_request(RecallRequest(query="observation", limit=2))

        assert result is not None
        self.assertEqual(result.metadata["found"], 2)
        self.assertFalse(result.metadata["diagnostics"]["candidate_limit_reached"])

    def test_recall_stays_within_its_token_budget(self) -> None:
        for index in range(20):
            self._add("relative volume " + "detail " * 60 + str(index))
        provider = KnowledgeRecallProvider(self.provider.retriever, token_budget=200)

        result = provider.execute_detailed_request(RecallRequest(query="relative volume", limit=20))

        assert result is not None
        self.assertLessEqual(result.metadata["diagnostics"]["estimated_tokens"], 200)
        self.assertGreater(result.metadata["diagnostics"]["dropped_for_budget"], 0)

    def test_the_wrong_request_type_is_refused(self) -> None:
        with self.assertRaises(ProviderExecutionError):
            self.provider.execute_detailed_request({"query": "not a request object"})


class RouterRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        database = SQLiteDatabase(Path(self._tmp.name) / "knowledge.db")
        self.graph = KnowledgeGraph(database)
        self.provider = KnowledgeRecallProvider(KnowledgeRetriever(database))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_registering_adds_it_to_what_the_planner_is_offered(self) -> None:
        router = GeneralKnowledgeRouter({})
        self.assertNotIn("recall", [item.name for item in router.capability_definitions()])

        router.register(self.provider)

        self.assertIn("recall", [item.name for item in router.capability_definitions()])

    def test_the_router_can_build_its_request(self) -> None:
        router = GeneralKnowledgeRouter({})
        router.register(self.provider)

        request = router.build_capability_request("recall", {"query": "relative volume"})

        self.assertIsInstance(request, RecallRequest)

    def test_the_router_executes_it(self) -> None:
        self.graph.records.add(MemoryRecord(
            kind=MemoryKind.OBSERVATION, topic=TOPIC, source="scanner",
            content="ABC showed relative volume acceleration"))
        router = GeneralKnowledgeRouter({})
        router.register(self.provider)

        result = router.execute_capability_request(
            "recall", router.build_capability_request("recall", {"query": "relative volume"})
        )

        assert result is not None
        self.assertIn("ABC showed relative volume", result.response or "")

    def test_a_duplicate_name_is_refused(self) -> None:
        router = GeneralKnowledgeRouter({})
        router.register(self.provider)

        with self.assertRaises(ValueError):
            router.register(self.provider)

    def test_free_text_routing_never_selects_it(self) -> None:
        """It has no can_handle, so only a planned call reaches it."""
        router = GeneralKnowledgeRouter({})
        router.register(self.provider)

        routed = router.route("what do we know about relative volume")

        self.assertNotEqual(getattr(routed, "provider", None), "recall")


if __name__ == "__main__":
    unittest.main()
