from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.knowledge import (
    KnowledgeQuery,
    KnowledgeRepository,
    KnowledgeRetriever,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    tokenize,
)
from core.knowledge.ranking import fit_to_budget, overlap, recency_weight, score
from core.storage.sqlite_database import SQLiteDatabase

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def _record(content: str, **overrides: object) -> MemoryRecord:
    values: dict[str, object] = {
        "kind": MemoryKind.OBSERVATION,
        "topic": "trading/candidates",
        "content": content,
        "source": "scanner",
    }
    values.update(overrides)
    return MemoryRecord(**values)  # type: ignore[arg-type]


class TokenizerTests(unittest.TestCase):
    def test_numbers_are_content_not_noise(self) -> None:
        """An observation is mostly numbers; dropping them would erase the signal."""
        tokens = tokenize("ABC had RSI 47 and volume acceleration 2.1x")

        self.assertIn("47", tokens)
        self.assertIn("2.1", tokens)
        self.assertIn("abc", tokens)
        self.assertIn("rsi", tokens)

    def test_common_words_are_dropped(self) -> None:
        self.assertEqual(tokenize("the price of the stock"), {"price", "stock"})

    def test_empty_text_yields_nothing(self) -> None:
        self.assertEqual(tokenize(""), set())

    def test_overlap_measures_coverage_of_the_query(self) -> None:
        """A long record should not be punished for saying more than was asked."""
        query = tokenize("relative volume")
        short = tokenize("relative volume")
        long = tokenize("relative volume rose sharply through the opening range and held")

        self.assertEqual(overlap(query, short), 1.0)
        self.assertEqual(overlap(query, long), 1.0)
        self.assertEqual(overlap(query, tokenize("something else entirely")), 0.0)


class RankingTests(unittest.TestCase):
    def test_recency_decays_by_half_life(self) -> None:
        fresh = _record("a", created_at=NOW.isoformat())
        month_old = _record("a", created_at=(NOW - timedelta(days=30)).isoformat())
        ancient = _record("a", created_at=(NOW - timedelta(days=300)).isoformat())

        self.assertAlmostEqual(recency_weight(fresh, now=NOW), 1.0, places=3)
        self.assertAlmostEqual(recency_weight(month_old, now=NOW), 0.5, places=3)
        self.assertLess(recency_weight(ancient, now=NOW), 0.01)

    def test_occurred_at_outranks_created_at_for_recency(self) -> None:
        """Backfilled history should age from when it happened."""
        backfilled = _record(
            "a", created_at=NOW.isoformat(), occurred_at=(NOW - timedelta(days=300)).isoformat()
        )
        self.assertLess(recency_weight(backfilled, now=NOW), 0.01)

    def test_standing_breaks_ties_between_equal_matches(self) -> None:
        query = tokenize("volume predicts")
        accepted = _record("volume predicts outcomes", kind=MemoryKind.KNOWLEDGE, status=MemoryStatus.ACCEPTED,
                           created_at=NOW.isoformat())
        proposed = _record("volume predicts outcomes", kind=MemoryKind.HYPOTHESIS, status=MemoryStatus.PROPOSED,
                           created_at=NOW.isoformat())

        self.assertGreater(score(accepted, query, now=NOW).score, score(proposed, query, now=NOW).score)

    def test_scores_carry_their_parts(self) -> None:
        scored = score(_record("relative volume", created_at=NOW.isoformat()), tokenize("volume"), now=NOW)

        self.assertEqual(set(scored.reasons), {"text", "recency", "status"})
        self.assertAlmostEqual(sum(scored.reasons.values()), scored.score, places=4)

    def test_budget_truncates_rather_than_overflowing(self) -> None:
        scored = [score(_record("x" * 200), set()) for _ in range(10)]

        kept, spent = fit_to_budget(scored, max_tokens=120)

        self.assertEqual(len(kept), 2)
        self.assertLessEqual(spent, 120)

    def test_no_budget_keeps_everything(self) -> None:
        scored = [score(_record("x" * 200), set()) for _ in range(10)]
        kept, _ = fit_to_budget(scored, max_tokens=None)
        self.assertEqual(len(kept), 10)


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        database = SQLiteDatabase(Path(self._tmp.name) / "knowledge.db")
        self.records = KnowledgeRepository(database)
        self.retriever = KnowledgeRetriever(database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_filters_narrow_before_ranking(self) -> None:
        self.records.add(_record("ABC relative volume 2.1x"))
        self.records.add(_record("DEF relative volume 1.9x", topic="trading/winners"))
        self.records.add(_record("GHI relative volume 3.0x", kind=MemoryKind.OUTCOME))

        result = self.retriever.retrieve(
            KnowledgeQuery(text="relative volume", topic="trading/candidates", kinds=(MemoryKind.OBSERVATION,))
        )

        self.assertEqual([item.record.content for item in result.records], ["ABC relative volume 2.1x"])

    def test_a_topic_prefix_spans_its_subtopics(self) -> None:
        self.records.add(_record("a", topic="trading/candidates"))
        self.records.add(_record("b", topic="trading/winners"))
        self.records.add(_record("c", topic="home/network"))

        result = self.retriever.retrieve(KnowledgeQuery(topic_prefix="trading/"))

        self.assertEqual(len(result.records), 2)

    def test_superseded_records_are_hidden_by_default(self) -> None:
        original = self.records.add(_record("v1"))
        self.records.supersede(original.id, _record("v2"))

        visible = self.retriever.retrieve(KnowledgeQuery(topic="trading/candidates"))
        everything = self.retriever.retrieve(KnowledgeQuery(topic="trading/candidates", include_superseded=True))

        self.assertEqual([item.record.content for item in visible.records], ["v2"])
        self.assertEqual(len(everything.records), 2)

    def test_a_time_window_uses_when_things_happened(self) -> None:
        self.records.add(_record("old", occurred_at="2026-01-01T00:00:00+00:00"))
        self.records.add(_record("recent", occurred_at="2026-09-01T00:00:00+00:00"))

        result = self.retriever.retrieve(
            KnowledgeQuery(topic="trading/candidates", occurred_after="2026-06-01T00:00:00+00:00")
        )

        self.assertEqual([item.record.content for item in result.records], ["recent"])

    def test_relevance_orders_the_results(self) -> None:
        self.records.add(_record("unrelated chatter about nothing"))
        self.records.add(_record("ABC showed relative volume acceleration"))

        result = self.retriever.retrieve(KnowledgeQuery(text="relative volume acceleration"))

        self.assertEqual(result.records[0].record.content, "ABC showed relative volume acceleration")

    def test_the_limit_caps_what_comes_back(self) -> None:
        for index in range(20):
            self.records.add(_record(f"observation {index}"))

        result = self.retriever.retrieve(KnowledgeQuery(topic="trading/candidates", limit=5))

        self.assertEqual(len(result.records), 5)

    def test_diagnostics_explain_the_result(self) -> None:
        for index in range(5):
            self.records.add(_record(f"relative volume {index}"))

        result = self.retriever.retrieve(KnowledgeQuery(text="relative volume", limit=2))

        self.assertEqual(result.diagnostics["candidates_examined"], 5)
        self.assertEqual(result.diagnostics["returned"], 2)
        self.assertFalse(result.diagnostics["candidate_limit_reached"])
        self.assertEqual(result.diagnostics["query_tokens"], ["relative", "volume"])
        self.assertEqual(len(result.diagnostics["top_scores"]), 2)

    def test_hitting_the_candidate_limit_is_reported(self) -> None:
        """Silently ranking a truncated slice would look like a confident answer."""
        for index in range(10):
            self.records.add(_record(f"observation {index}"))

        result = self.retriever.retrieve(KnowledgeQuery(candidate_limit=4))

        self.assertTrue(result.diagnostics["candidate_limit_reached"])

    def test_relevance_beyond_the_candidate_window_is_missed(self) -> None:
        """A known limit of ranking a recency-bounded slice, pinned so it stays known.

        Candidates are taken newest-first, so a strong match older than the
        window never reaches the ranker. candidate_limit_reached is the only
        signal that this could have happened, which is why it is reported.
        """
        gold = self.records.add(_record("ZZZ showed extraordinary relative volume acceleration 9.9x"))
        for index in range(30):
            self.records.add(_record(f"routine note {index}"))

        query = KnowledgeQuery(text="extraordinary relative volume acceleration 9.9x")
        found = self.retriever.retrieve(query)
        missed = self.retriever.retrieve(KnowledgeQuery(text=query.text, candidate_limit=10))

        self.assertIn(gold.id, [item.record.id for item in found.records])
        self.assertNotIn(gold.id, [item.record.id for item in missed.records])
        self.assertTrue(missed.diagnostics["candidate_limit_reached"])

    def test_budget_pressure_is_reported(self) -> None:
        for index in range(5):
            self.records.add(_record("x" * 400))

        result = self.retriever.retrieve(KnowledgeQuery(limit=5, max_tokens=150))

        self.assertGreater(result.diagnostics["dropped_for_budget"], 0)
        self.assertLessEqual(result.diagnostics["estimated_tokens"], 150)

    def test_similar_to_excludes_the_record_itself(self) -> None:
        subject = self.records.add(_record("ABC showed relative volume acceleration 2.1x"))
        self.records.add(_record("DEF showed relative volume acceleration 1.9x"))
        self.records.add(_record("totally unrelated note"))

        result = self.retriever.similar_to(subject, limit=5)

        ids = [item.record.id for item in result.records]
        self.assertNotIn(subject.id, ids)
        self.assertEqual(result.records[0].record.content, "DEF showed relative volume acceleration 1.9x")

    def test_empty_store_retrieves_nothing_without_error(self) -> None:
        result = self.retriever.retrieve(KnowledgeQuery(text="anything"))

        self.assertEqual(result.records, [])
        self.assertEqual(result.diagnostics["candidates_examined"], 0)

    def test_context_rendering_is_ordered_and_terse(self) -> None:
        self.records.add(_record("unrelated chatter"))
        self.records.add(_record("ABC relative volume"))

        text = self.retriever.retrieve(KnowledgeQuery(text="relative volume")).as_context()

        self.assertTrue(text.startswith("- ABC relative volume"))


class RetrievalIndexTests(unittest.TestCase):
    """Retrieval must filter in SQL. A table scan here is the failure mode."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self._tmp.name) / "knowledge.db")
        self.records = KnowledgeRepository(self.database)
        self.retriever = KnowledgeRetriever(self.database)
        for index in range(60):
            self.records.add(_record(f"observation {index}"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan(self, query: KnowledgeQuery) -> str:
        from core.knowledge.retrieval import _filters

        clauses, params = _filters(query)
        params.append(query.candidate_limit)
        with self.database.connect() as conn:
            rows = conn.execute(
                f"EXPLAIN QUERY PLAN SELECT id FROM memories WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, sequence DESC LIMIT ?",
                params,
            ).fetchall()
        return " ".join(str(row[-1]) for row in rows)

    def test_topic_and_kind_use_the_index(self) -> None:
        plan = self._plan(KnowledgeQuery(topic="trading/candidates", kinds=(MemoryKind.OBSERVATION,)))

        self.assertIn("idx_memories_topic_kind", plan)
        self.assertNotIn("SCAN memories", plan.upper())

    def test_a_topic_prefix_uses_the_index_rather_than_scanning(self) -> None:
        """LIKE would be case-insensitive and unindexed; the range form is why."""
        plan = self._plan(KnowledgeQuery(topic_prefix="trading/"))

        self.assertIn("idx_memories_topic_kind", plan)
        self.assertNotIn("SCAN memories", plan.upper())


if __name__ == "__main__":
    unittest.main()
