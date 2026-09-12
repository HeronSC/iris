# File: core/tests/test_memory_facts.py

"""Conflicting facts, supersession, retiring and pruning (2.1): which one wins, and the old one stays."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.assistant.knowledge_commands import KnowledgeCommandHandler
from core.assistant.knowledge_provider import KnowledgeRecallProvider, RecallRequest
from core.knowledge.facts import FactService, find_conflicts, similarity
from core.knowledge.graph import KnowledgeGraph
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.repository import KnowledgeRepository
from core.knowledge.retrieval import KnowledgeQuery, KnowledgeRetriever
from core.knowledge.review import KnowledgeReviewWorkflow
from core.storage.sqlite_database import SQLiteDatabase


class FactServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "knowledge.db")
        self.repository = KnowledgeRepository(self.database)
        self.facts = FactService(self.repository)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_similar_facts_on_a_topic_are_reported_and_the_newer_one_ranks_first(self) -> None:
        first, conflicts = self.facts.record("bc/dispatch", "The dispatch pool page is 99510", source="user")
        self.assertEqual(conflicts, [])
        second, conflicts = self.facts.record("bc/dispatch", "The dispatch pool page is 99512", source="user")
        self.assertEqual([item.record.id for item in conflicts], [first.id])
        self.assertGreaterEqual(conflicts[0].similarity, 0.5)
        unrelated, conflicts = self.facts.record("bc/dispatch", "Drivers are assigned from the pool", source="user")
        self.assertEqual(conflicts, [])
        with self.assertRaises(KnowledgeError):
            self.facts.record("bc/dispatch", "the dispatch pool page is 99512", source="user")
        result = KnowledgeRetriever(self.database).retrieve(KnowledgeQuery(text="dispatch pool page", limit=5))
        self.assertEqual(result.records[0].record.id, second.id)
        self.assertLess(similarity("a b c", "x y z"), 0.01)

    def test_supersede_keeps_the_old_record_as_history_and_hides_it_from_recall(self) -> None:
        old, _conflicts = self.facts.record("bc/dispatch", "The dispatch pool page is 99510", source="user", scope="project:mammoth")
        new = self.facts.supersede(old.id, "The dispatch pool page is 99511", source="user")
        self.assertEqual(new.scope, "project:mammoth")
        self.assertEqual(new.supersedes, old.id)
        self.assertEqual(self.repository.get(old.id).status, MemoryStatus.SUPERSEDED)
        self.assertEqual([record.id for record in self.repository.list_by_topic("bc/dispatch")], [new.id])
        self.assertEqual(find_conflicts(self.repository, "bc/dispatch", "The dispatch pool page is 99510"), [])

    def test_retire_needs_a_reason_and_hides_the_record_everywhere(self) -> None:
        record, _conflicts = self.facts.record("bc/dispatch", "Dispatch runs at 6 am", source="user")
        with self.assertRaises(KnowledgeError):
            self.facts.retire(record.id, reason="  ")
        retired = self.facts.retire(record.id, reason="the schedule moved")
        self.assertEqual(retired.status, MemoryStatus.RETIRED)
        self.assertEqual(self.repository.list_all(), [])
        self.assertEqual(len(self.repository.list_all(include_superseded=True)), 1)
        self.assertEqual(self.repository.recent(), [])
        self.assertEqual(self.repository.count_by_scope(), {})
        self.assertEqual(KnowledgeRetriever(self.database).retrieve(KnowledgeQuery(text="dispatch", limit=5)).records, [])
        provider = KnowledgeRecallProvider(KnowledgeRetriever(self.database))
        self.assertEqual(provider.execute_detailed_request(RecallRequest(query="dispatch", limit=5)).metadata["found"], 0)
        with self.assertRaises(KnowledgeError):
            self.facts.retire(record.id, reason="again")

    def test_prune_retires_only_old_observations_and_outcomes(self) -> None:
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        self.repository.add_many(
            [
                MemoryRecord(kind=MemoryKind.OBSERVATION, topic="trading/candidates", content="Saw a breakout", source="bot", occurred_at=old_stamp),
                MemoryRecord(kind=MemoryKind.OUTCOME, topic="trading/candidates", content="It faded", source="bot", occurred_at=old_stamp),
                MemoryRecord(kind=MemoryKind.FACT, topic="trading/rules", content="Never trade the open", source="user", created_at=old_stamp),
                MemoryRecord(kind=MemoryKind.OBSERVATION, topic="trading/candidates", content="Saw a fresh breakout", source="bot"),
            ]
        )
        candidates = self.facts.prune_candidates(90)
        self.assertEqual(sorted(item.content for item in candidates), ["It faded", "Saw a breakout"])
        retired = self.facts.prune(90)
        self.assertEqual(len(retired), 2)
        remaining = sorted(item.content for item in self.repository.list_all())
        self.assertEqual(remaining, ["Never trade the open", "Saw a fresh breakout"])
        with self.assertRaises(KnowledgeError):
            self.facts.prune_candidates(0)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "knowledge.db")
        self.graph = KnowledgeGraph(self.database)
        self.lines: list[str] = []
        self.handler = KnowledgeCommandHandler(KnowledgeReviewWorkflow(HypothesisTracker(self.graph)), output=lambda text, role=None: self.lines.append(text))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, command: str) -> str:
        del self.lines[:]
        self.assertTrue(self.handler.handle(command, {}))
        return "\n".join(self.lines)

    def test_fact_supersede_forget_browse_and_prune(self) -> None:
        self.assertIn("Recorded fact", self._run("/knowledge fact bc/dispatch The dispatch pool page is 99510"))
        conflict = self._run("/knowledge fact bc/dispatch The dispatch pool page is 99512")
        self.assertIn("may conflict with 1 earlier fact", conflict)
        self.assertIn("/knowledge supersede", conflict)
        first = self.graph.records.list_by_topic("bc/dispatch")[-1]
        superseded = self._run(f"/knowledge supersede {first.id[:8]} The dispatch pool page is 99513")
        self.assertIn("now replaces", superseded)
        self.assertIn("was: The dispatch pool page is 99510", superseded)
        browse = self._run("/knowledge browse bc/dispatch")
        self.assertIn("2 records in bc/dispatch", browse)
        self.assertNotIn("99510", browse)
        newest = self.graph.records.recent(limit=1)[0]
        forgotten = self._run(f"/knowledge forget {newest.id[:8]} it was a typo")
        self.assertIn("Retired", forgotten)
        self.assertIn("why: it was a typo", forgotten)
        self.assertIn("1 record in bc/dispatch", self._run("/knowledge browse bc/dispatch"))
        self.assertIn("Nothing to prune", self._run("/knowledge prune 30"))
        self.assertTrue(self._run("/knowledge forget").startswith("Usage"))
        self.assertIn("Usage: /knowledge prune", self._run("/knowledge prune soon"))
        self.assertIn("newest first", self._run("/knowledge browse"))


if __name__ == "__main__":
    unittest.main()
