from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.knowledge import (
    KnowledgeError,
    KnowledgeRepository,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
)
from core.storage.sqlite_database import SQLiteDatabase


def _observation(**overrides: object) -> MemoryRecord:
    values: dict[str, object] = {
        "kind": MemoryKind.OBSERVATION,
        "topic": "trading/candidates",
        "content": "ABC had RSI 47 and volume acceleration 2.1x at 10:04.",
        "data": {"symbol": "ABC", "rsi": 47, "volume_acceleration": 2.1},
        "source": "scanner",
        "source_ref": "run-2026-09-06",
        "occurred_at": "2026-09-06T14:04:00+00:00",
    }
    values.update(overrides)
    return MemoryRecord(**values)  # type: ignore[arg-type]


class KnowledgeRecordTests(unittest.TestCase):
    def test_status_defaults_by_kind(self) -> None:
        """A thing we saw arrives believed; a thing we suspect has to earn it."""
        self.assertEqual(_observation().status, MemoryStatus.OBSERVED)
        hypothesis = MemoryRecord(
            kind=MemoryKind.HYPOTHESIS,
            topic="trading/candidates",
            content="Rising relative volume in the first 45 minutes may predict outperformance.",
            source="analysis",
        )
        self.assertEqual(hypothesis.status, MemoryStatus.PROPOSED)

    def test_provenance_is_required(self) -> None:
        with self.assertRaises(KnowledgeError):
            _observation(source="  ")
        with self.assertRaises(KnowledgeError):
            _observation(content="")
        with self.assertRaises(KnowledgeError):
            _observation(topic="")

    def test_confidence_must_be_a_probability(self) -> None:
        for bad in (-0.1, 1.5):
            with self.assertRaises(KnowledgeError):
                _observation(confidence=bad)
        self.assertEqual(_observation(confidence=0.5).confidence, 0.5)

    def test_string_kind_and_status_are_coerced(self) -> None:
        record = _observation(kind="outcome", status="accepted")
        self.assertIs(record.kind, MemoryKind.OUTCOME)
        self.assertIs(record.status, MemoryStatus.ACCEPTED)

    def test_ids_are_unique_without_a_clock(self) -> None:
        ids = {_observation().id for _ in range(200)}
        self.assertEqual(len(ids), 200)


class KnowledgeRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repository = KnowledgeRepository(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_every_column_survives_a_round_trip(self) -> None:
        stored = self.repository.add(_observation(confidence=0.75))

        loaded = self.repository.get(stored.id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.kind, MemoryKind.OBSERVATION)
        self.assertEqual(loaded.topic, "trading/candidates")
        self.assertEqual(loaded.status, MemoryStatus.OBSERVED)
        self.assertEqual(loaded.content, stored.content)
        self.assertEqual(loaded.data, {"symbol": "ABC", "rsi": 47, "volume_acceleration": 2.1})
        self.assertEqual(loaded.confidence, 0.75)
        self.assertEqual(loaded.source, "scanner")
        self.assertEqual(loaded.source_ref, "run-2026-09-06")
        self.assertEqual(loaded.occurred_at, "2026-09-06T14:04:00+00:00")
        self.assertEqual(loaded.created_at, stored.created_at)
        self.assertIsNone(loaded.supersedes)
        self.assertIsNone(loaded.superseded_by)

    def test_missing_memory_reads_as_none(self) -> None:
        self.assertIsNone(self.repository.get("nope"))

    def test_ids_cannot_be_reused(self) -> None:
        stored = self.repository.add(_observation())
        with self.assertRaises(KnowledgeError):
            self.repository.add(_observation(id=stored.id))

    def test_superseding_keeps_the_original_readable(self) -> None:
        """The audit trail is the point: the old belief must stay on disk."""
        original = self.repository.add(_observation(content="ABC had RSI 47.", confidence=0.5))
        corrected = self.repository.supersede(
            original.id,
            _observation(content="ABC had RSI 52; the earlier reading was stale.", confidence=0.9),
        )

        old = self.repository.get(original.id)
        new = self.repository.get(corrected.id)
        assert old is not None and new is not None
        self.assertEqual(old.content, "ABC had RSI 47.", "superseding must not rewrite history")
        self.assertEqual(old.status, MemoryStatus.SUPERSEDED)
        self.assertEqual(old.superseded_by, corrected.id)
        self.assertEqual(new.supersedes, original.id)
        self.assertEqual(new.status, MemoryStatus.OBSERVED)

    def test_history_reads_oldest_first(self) -> None:
        first = self.repository.add(_observation(content="v1"))
        second = self.repository.supersede(first.id, _observation(content="v2"))
        third = self.repository.supersede(second.id, _observation(content="v3"))

        chain = self.repository.history(third.id)

        self.assertEqual([item.content for item in chain], ["v1", "v2", "v3"])

    def test_a_memory_cannot_be_superseded_twice(self) -> None:
        original = self.repository.add(_observation(content="v1"))
        self.repository.supersede(original.id, _observation(content="v2"))

        with self.assertRaises(KnowledgeError):
            self.repository.supersede(original.id, _observation(content="rival v2"))

    def test_superseding_a_missing_memory_is_refused(self) -> None:
        with self.assertRaises(KnowledgeError):
            self.repository.supersede("nope", _observation())

    def test_a_memory_cannot_supersede_itself(self) -> None:
        stored = self.repository.add(_observation())
        with self.assertRaises(KnowledgeError):
            self.repository.supersede(stored.id, _observation(id=stored.id))

    def test_a_failed_supersede_leaves_nothing_behind(self) -> None:
        original = self.repository.add(_observation(content="v1"))
        clash = self.repository.add(_observation(content="unrelated"))

        with self.assertRaises(KnowledgeError):
            self.repository.supersede(original.id, _observation(id=clash.id, content="v2"))

        untouched = self.repository.get(original.id)
        assert untouched is not None
        self.assertEqual(untouched.status, MemoryStatus.OBSERVED)
        self.assertIsNone(untouched.superseded_by)
        self.assertEqual(self.repository.count(), 2)

    def test_set_status_moves_a_hypothesis_along(self) -> None:
        hypothesis = self.repository.add(
            MemoryRecord(
                kind=MemoryKind.HYPOTHESIS,
                topic="trading/candidates",
                content="Rising relative volume may predict outperformance.",
                source="analysis",
            )
        )

        self.repository.set_status(hypothesis.id, MemoryStatus.TESTING)

        loaded = self.repository.get(hypothesis.id)
        assert loaded is not None
        self.assertEqual(loaded.status, MemoryStatus.TESTING)
        self.assertEqual(loaded.content, hypothesis.content, "status changes must not touch content")

    def test_set_status_on_a_missing_memory_is_refused(self) -> None:
        with self.assertRaises(KnowledgeError):
            self.repository.set_status("nope", MemoryStatus.TESTING)

    def test_listing_filters_by_topic_and_kind(self) -> None:
        self.repository.add(_observation(topic="trading/candidates", content="a"))
        self.repository.add(_observation(topic="trading/candidates", content="b", kind=MemoryKind.OUTCOME))
        self.repository.add(_observation(topic="home/network", content="c"))

        candidates = self.repository.list_by_topic("trading/candidates")
        outcomes = self.repository.list_by_topic("trading/candidates", kind=MemoryKind.OUTCOME)

        self.assertEqual(len(candidates), 2)
        self.assertEqual([item.content for item in outcomes], ["b"])
        self.assertEqual(len(self.repository.list_by_topic("nothing/here")), 0)

    def test_listing_hides_superseded_records_unless_asked(self) -> None:
        original = self.repository.add(_observation(content="v1"))
        self.repository.supersede(original.id, _observation(content="v2"))

        self.assertEqual([item.content for item in self.repository.list_by_topic("trading/candidates")], ["v2"])
        self.assertEqual(
            len(self.repository.list_by_topic("trading/candidates", include_superseded=True)), 2
        )

    def test_listing_respects_its_limit(self) -> None:
        for index in range(10):
            self.repository.add(_observation(content=f"row {index}"))

        self.assertEqual(len(self.repository.list_by_topic("trading/candidates", limit=3)), 3)

    def test_ordering_is_stable_when_created_at_ties(self) -> None:
        """created_at is second-resolution, so ties are normal and must not reorder."""
        same_second = "2026-09-06T14:04:00+00:00"
        for index in range(5):
            self.repository.add(_observation(content=f"row {index}", created_at=same_second))

        first = [item.content for item in self.repository.list_by_topic("trading/candidates")]
        second = [item.content for item in self.repository.list_by_topic("trading/candidates")]

        self.assertEqual(first, second, "repeated listings must agree")
        self.assertEqual(first, ["row 4", "row 3", "row 2", "row 1", "row 0"])

    def test_backdated_records_sort_by_when_they_happened(self) -> None:
        """Loading history later must not make old records look like the newest."""
        self.repository.add(_observation(content="today", created_at="2026-09-06T00:00:00+00:00"))
        self.repository.add(_observation(content="backfilled", created_at="2020-01-01T00:00:00+00:00"))

        listed = [item.content for item in self.repository.list_by_topic("trading/candidates")]

        self.assertEqual(listed, ["today", "backfilled"])

    def test_listing_is_served_by_an_index_not_a_sort(self) -> None:
        """Slice 3 scales only if the ORDER BY is covered; a temp b-tree means it is not."""
        for index in range(50):
            self.repository.add(_observation(content=f"row {index}"))

        with self.repository.database.connect() as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM memories "
                "WHERE topic = ? AND kind = ? AND status != ? "
                "ORDER BY created_at DESC, sequence DESC LIMIT ?",
                ("trading/candidates", "observation", "superseded", 20),
            ).fetchall()

        detail = " ".join(str(row[-1]) for row in plan)
        self.assertIn("idx_memories_topic_kind", detail)
        self.assertNotIn("TEMP B-TREE", detail.upper(), f"query fell back to sorting: {detail}")

    def test_schema_survives_reopening_the_database(self) -> None:
        stored = self.repository.add(_observation())
        reopened = KnowledgeRepository(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))

        self.assertIsNotNone(reopened.get(stored.id))
        self.assertEqual(reopened.count(), 1)


if __name__ == "__main__":
    unittest.main()
