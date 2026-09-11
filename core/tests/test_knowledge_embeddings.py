# File: core/tests/test_knowledge_embeddings.py

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from core.knowledge import (
    EmbeddingConfig,
    KnowledgeQuery,
    KnowledgeRepository,
    KnowledgeRetriever,
    MemoryEmbeddingIndex,
    MemoryKind,
    MemoryRecord,
)
from core.knowledge.embeddings import DOCUMENT_PREFIX, QUERY_PREFIX, embedding_text
from core.knowledge.ranking import score
from core.llm.ollama_client import OllamaClientError
from core.storage.sqlite_database import SQLiteDatabase

DIMS = 4

#: A tiny "language": every phrase maps to a direction. Phrases that mean the
#: same thing share a direction; unrelated ones are orthogonal.
_MEANINGS = {
    "revenue grew": (1.0, 0.0, 0.0, 0.0),
    "sales increased": (0.95, 0.05, 0.0, 0.0),
    "income went up": (0.9, 0.1, 0.0, 0.0),
    "the printer jammed": (0.0, 0.0, 1.0, 0.0),
    "paper stuck in the copier": (0.0, 0.0, 0.95, 0.05),
    "volume spike": (0.0, 1.0, 0.0, 0.0),
}


def _unit(vector: tuple[float, ...]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise OllamaClientError("Ollama connection failed")
        vectors: list[list[float]] = []
        for text in texts:
            body = text.removeprefix(DOCUMENT_PREFIX).removeprefix(QUERY_PREFIX).lower()
            direction = (0.0, 0.0, 0.0, 1.0)
            for phrase, meaning in _MEANINGS.items():
                if phrase in body:
                    direction = meaning
                    break
            vectors.append(_unit(direction))
        return vectors


def _record(content: str, **overrides: object) -> MemoryRecord:
    values: dict[str, object] = {"kind": MemoryKind.FACT, "topic": "finance", "content": content, "source": "user"}
    values.update(overrides)
    return MemoryRecord(**values)  # type: ignore[arg-type]


class EmbeddingIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self._tmp.name) / "knowledge.db")
        self.records = KnowledgeRepository(self.database)
        self.embedder = FakeEmbedder()
        self.config = EmbeddingConfig(model="fake", dimensions=DIMS, batch_size=2)
        self.index = MemoryEmbeddingIndex(self.database, self.embedder, self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extension_loads_and_schema_exists(self) -> None:
        self.assertTrue(self.index.available)
        self.assertEqual(self.index.count(), 0)
        status = self.index.status()
        self.assertEqual(status["model"], "fake")
        self.assertEqual(status["pending"], 0)

    def test_pending_skips_machine_sources_and_indexing_catches_up(self) -> None:
        self.records.add(_record("revenue grew"))
        self.records.add(_record("volume spike", kind=MemoryKind.OBSERVATION, source="bot:scanner", topic="trading"))
        self.records.add(_record("REJECT: weak momentum", kind=MemoryKind.DECISION, source="iris:shadow-2", topic="trading"))
        self.records.add(_record("the printer jammed", kind=MemoryKind.OBSERVATION, source="user", topic="office"))
        self.records.add(_record("sales increased"))

        pending = self.index.pending()
        self.assertEqual([text for _, text in pending], ["finance: revenue grew", "office: the printer jammed", "finance: sales increased"])
        self.assertEqual(self.index.index_pending(), 3)
        self.assertEqual(self.index.count(), 3)
        self.assertEqual(len(self.embedder.calls), 2, "batches of two")
        self.assertTrue(all(text.startswith(DOCUMENT_PREFIX) for call in self.embedder.calls for text in call))
        self.assertEqual(self.index.index_pending(), 0)
        self.assertIsNone(self.index.last_error)

    def test_embedder_failure_stops_the_pass_and_is_reported(self) -> None:
        self.records.add(_record("revenue grew"))
        self.embedder.fail = True
        self.assertEqual(self.index.index_pending(), 0)
        self.assertIn("connection failed", self.index.last_error or "")
        self.embedder.fail = False
        self.assertEqual(self.index.index_pending(), 1)

    def test_search_returns_nearest_first(self) -> None:
        stored = [self.records.add(_record(text)) for text in ("revenue grew", "the printer jammed", "volume spike")]
        self.index.index_pending()
        hits = self.index.search("income went up", k=3)
        self.assertEqual(len(hits), 3)
        best_sequence, best_similarity = hits[0]
        self.assertGreater(best_similarity, 0.95)
        with self.database.connect() as conn:
            row = conn.execute("SELECT id FROM memories WHERE sequence = ?", (best_sequence,)).fetchone()
        self.assertEqual(row[0], stored[0].id)
        self.assertTrue(self.embedder.calls[-1][0].startswith(QUERY_PREFIX))

    def test_model_change_rebuilds_the_table(self) -> None:
        self.records.add(_record("revenue grew"))
        self.index.index_pending()
        self.assertEqual(self.index.count(), 1)
        other = MemoryEmbeddingIndex(self.database, self.embedder, EmbeddingConfig(model="other", dimensions=DIMS))
        self.assertEqual(other.count(), 0)
        self.assertEqual(len(other.pending()), 1)

    def test_disabled_or_missing_embedder_means_unavailable(self) -> None:
        self.assertFalse(MemoryEmbeddingIndex(self.database, None, self.config).available)
        off = MemoryEmbeddingIndex(self.database, self.embedder, EmbeddingConfig(enabled=False, dimensions=DIMS))
        self.assertFalse(off.available)
        self.assertEqual(off.index_pending(), 0)
        self.assertEqual(off.search("anything", k=3), [])

    def test_config_reads_the_knowledge_block_and_routed_model(self) -> None:
        config = EmbeddingConfig.from_config(
            {
                "models": {"tasks": {"embedding": "nomic-embed-text"}},
                "knowledge": {"embeddings": {"batch_size": 8, "skip_sources": ["bot:", "sim:"]}},
            }
        )
        self.assertEqual(config.model, "nomic-embed-text")
        self.assertEqual(config.batch_size, 8)
        self.assertEqual(config.skip_sources, ("bot:", "sim:"))
        self.assertEqual(config.relevance(config.similarity_floor), 0.0)
        self.assertEqual(config.relevance(config.similarity_ceiling), 1.0)
        self.assertAlmostEqual(config.relevance((config.similarity_floor + config.similarity_ceiling) / 2), 0.5)
        self.assertEqual(config.relevance(0.0), 0.0)

    def test_embedding_text_puts_the_topic_into_words(self) -> None:
        self.assertEqual(embedding_text(_record("x", topic="trading/candidates")), "trading candidates: x")


class FusedRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self._tmp.name) / "knowledge.db")
        self.records = KnowledgeRepository(self.database)
        self.embedder = FakeEmbedder()
        self.index = MemoryEmbeddingIndex(self.database, self.embedder, EmbeddingConfig(model="fake", dimensions=DIMS))
        self.retriever = KnowledgeRetriever(self.database, embeddings=self.index)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_record_that_shares_no_words_is_found_by_meaning(self) -> None:
        self.records.add(_record("sales increased"))
        self.records.add(_record("the printer jammed", topic="office"))
        self.index.index_pending()

        result = self.retriever.retrieve(KnowledgeQuery(text="revenue grew"))
        contents = [item.record.content for item in result.records]
        self.assertEqual(contents, ["sales increased"])
        top = result.records[0]
        self.assertEqual(top.reasons["lexical"], 0.0)
        self.assertGreater(top.reasons["semantic"], 0.9)
        self.assertEqual(top.reasons["text"], top.reasons["semantic"])
        self.assertEqual(result.diagnostics["semantic_added"], 1)
        self.assertEqual(result.diagnostics["candidates_examined"], 1)

    def test_lexical_and_semantic_candidates_merge_without_duplicates(self) -> None:
        self.records.add(_record("revenue grew this quarter"))
        self.records.add(_record("sales increased"))
        self.index.index_pending()

        result = self.retriever.retrieve(KnowledgeQuery(text="revenue grew"))
        self.assertEqual(sorted(item.record.content for item in result.records), ["revenue grew this quarter", "sales increased"])
        self.assertEqual(result.diagnostics["candidates_examined"], 2)
        self.assertEqual(result.diagnostics["semantic_candidates"], 2)

    def test_filters_still_apply_to_semantic_candidates(self) -> None:
        self.records.add(_record("sales increased", topic="other"))
        self.index.index_pending()
        result = self.retriever.retrieve(KnowledgeQuery(text="revenue grew", topic="finance"))
        self.assertEqual(result.records, [])

    def test_unrelated_records_are_not_pulled_in_by_resemblance(self) -> None:
        self.records.add(_record("the printer jammed", topic="office"))
        self.index.index_pending()
        result = self.retriever.retrieve(KnowledgeQuery(text="revenue grew"))
        self.assertEqual(result.records, [])
        self.assertEqual(result.diagnostics["semantic_candidates"], 0)

    def test_embedder_outage_degrades_to_lexical_only(self) -> None:
        self.records.add(_record("revenue grew this quarter"))
        self.index.index_pending()
        self.embedder.fail = True
        result = self.retriever.retrieve(KnowledgeQuery(text="revenue grew"))
        self.assertEqual([item.record.content for item in result.records], ["revenue grew this quarter"])
        self.assertIn("semantic_error", result.diagnostics)
        self.assertNotIn("semantic", result.records[0].reasons)

    def test_a_browse_without_text_never_touches_the_index(self) -> None:
        self.records.add(_record("revenue grew"))
        self.index.index_pending()
        calls_before = len(self.embedder.calls)
        result = self.retriever.retrieve(KnowledgeQuery(topic="finance"))
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(self.embedder.calls), calls_before)

    def test_similar_to_uses_meaning_too(self) -> None:
        anchor = self.records.add(_record("revenue grew"))
        self.records.add(_record("income went up"))
        self.index.index_pending()
        result = self.retriever.similar_to(anchor, limit=3)
        self.assertEqual([item.record.content for item in result.records], ["income went up"])


class ScorerSemanticTests(unittest.TestCase):
    def test_semantic_only_when_given_and_text_is_the_stronger_signal(self) -> None:
        record = _record("sales increased")
        plain = score(record, {"revenue", "grew"})
        self.assertNotIn("semantic", plain.reasons)
        self.assertEqual(plain.reasons["text"], 0.0)
        fused = score(record, {"revenue", "grew"}, semantic=0.8)
        self.assertEqual(fused.reasons["text"], 0.8)
        self.assertEqual(fused.reasons["lexical"], 0.0)
        self.assertEqual(fused.reasons["semantic"], 0.8)
        self.assertAlmostEqual(fused.score, plain.score + 0.8, places=4)
        weaker = score(_record("revenue grew"), {"revenue", "grew"}, semantic=0.3)
        self.assertEqual(weaker.reasons["text"], 1.0)


if __name__ == "__main__":
    unittest.main()


class IsolatedWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self._tmp.name) / "knowledge.db")
        self.records = KnowledgeRepository(self.database)
        self.embedder = FakeEmbedder()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_in_process_writes_still_work_when_isolation_is_off(self) -> None:
        index = MemoryEmbeddingIndex(self.database, self.embedder, EmbeddingConfig(model="fake", dimensions=DIMS, isolate_writes=False))
        self.records.add(_record("revenue grew"))
        self.assertEqual(index.index_pending(), 1)
        self.assertEqual(index.count(), 1)
        self.assertGreater(index.search("sales increased", k=1)[0][1], 0.9)

    def test_a_failing_writer_is_reported_not_raised(self) -> None:
        index = MemoryEmbeddingIndex(self.database, self.embedder, EmbeddingConfig(model="fake", dimensions=DIMS))
        self.records.add(_record("revenue grew"))
        # Vectors of the wrong size make the child fail; the pass stops and says why.
        self.embedder.embed = lambda texts, model=None: [[0.1, 0.2] for _ in texts]  # type: ignore[method-assign]
        self.assertEqual(index.index_pending(), 0)
        self.assertIn("vector writer failed", index.last_error or "")
        self.assertEqual(index.count(), 0)
