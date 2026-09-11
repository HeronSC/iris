# File: core/tests/test_document_embeddings.py

from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.documents.catalog import DocumentCatalog
from core.documents.embeddings import DocumentEmbeddingConfig, DocumentEmbeddingIndex, chunk_text
from core.documents.query_parser import FileSearchQueryParser, QueryParserConfig
from core.documents.search_service import DocumentSearchService
from core.knowledge.embeddings import DOCUMENT_PREFIX, QUERY_PREFIX
from core.storage.sqlite_database import SQLiteDatabase
from core.storage.vector_table import VectorTable

DIMS = 4
_MEANINGS = {
    "invoice": (1.0, 0.0, 0.0, 0.0),
    "bill for services": (0.95, 0.05, 0.0, 0.0),
    "recipe": (0.0, 1.0, 0.0, 0.0),
    "cooking instructions": (0.0, 0.95, 0.05, 0.0),
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
            raise RuntimeError("Ollama is down")
        out: list[list[float]] = []
        for text in texts:
            body = text.removeprefix(DOCUMENT_PREFIX).removeprefix(QUERY_PREFIX).lower()
            direction = (0.0, 0.0, 0.0, 1.0)
            for phrase, meaning in _MEANINGS.items():
                if phrase in body:
                    direction = meaning
                    break
            out.append(_unit(direction))
        return out


def _document(path: str, text: str, *, status: str = "indexed", content_hash: str = "h1") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    name = Path(path).name
    return {
        "id": "file-" + name.replace(".", ""),
        "path": path,
        "name": name,
        "extension": Path(path).suffix.lower(),
        "size": len(text),
        "created_at": now,
        "modified_at": now,
        "indexed_at": now,
        "content_hash": content_hash,
        "content_status": status,
        "extracted_text": text,
        "extractor": "test",
        "error": None,
    }


class ChunkingTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self) -> None:
        chunks = chunk_text("hello world")
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].start, chunks[0].end, chunks[0].page), (0, 11, None))

    def test_long_text_is_windowed_with_overlap_on_boundaries(self) -> None:
        text = "\n".join(f"Line {index} of the document with some words in it." for index in range(60))
        chunks = chunk_text(text, chunk_chars=300, overlap=40)
        self.assertGreater(len(chunks), 5)
        for previous, current in zip(chunks, chunks[1:]):
            self.assertLess(current.start, previous.end, "windows overlap")
            self.assertTrue(text[previous.end - 1] == "\n" or previous.end == len(text), "cut on a line break")
        self.assertEqual(chunks[-1].end, len(text))

    def test_pages_are_carried_from_markers(self) -> None:
        text = "[page 1]\n" + ("a" * 500) + "\n[page 2]\n" + ("b" * 500) + "\n[page 3]\n" + ("c" * 100)
        chunks = chunk_text(text, chunk_chars=520, overlap=0)
        self.assertEqual([chunk.page for chunk in chunks], [1, 2, 3])

    def test_max_chunks_caps_the_work(self) -> None:
        self.assertEqual(len(chunk_text("x" * 100000, chunk_chars=1000, overlap=0, max_chunks=5)), 5)


class VectorTableTests(unittest.TestCase):
    def test_store_search_delete_and_rebuild_on_model_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = SQLiteDatabase(Path(tmp) / "v.db")
            table = VectorTable(database, "things_vec", DIMS, model_stamp="m1:4", isolate_writes=False)
            self.assertTrue(table.available)
            self.assertTrue(table.ensure_schema())
            table.store([(1, _unit((1, 0, 0, 0))), (2, _unit((0, 1, 0, 0)))])
            self.assertEqual(table.count(), 2)
            self.assertEqual(table.search(_unit((0.9, 0.1, 0, 0)), k=1)[0][0], 1)
            table.delete([1])
            self.assertEqual(table.keys(), {2})
            self.assertFalse(VectorTable(database, "things_vec", DIMS, model_stamp="m1:4").ensure_schema())
            self.assertTrue(VectorTable(database, "things_vec", DIMS, model_stamp="m2:4").ensure_schema())
            self.assertEqual(VectorTable(database, "things_vec", DIMS, model_stamp="m2:4").count(), 0)
            with self.assertRaises(ValueError):
                VectorTable(database, "bad name", DIMS, model_stamp="x")


class DocumentIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self._tmp.name) / "documents.db")
        self.catalog = DocumentCatalog(self.database)
        self.embedder = FakeEmbedder()
        self.config = DocumentEmbeddingConfig(model="fake", dimensions=DIMS, chunk_chars=300, chunk_overlap=20, batch_size=2)
        self.index = DocumentEmbeddingIndex(self.database, self.embedder, self.config)
        self.parser = FileSearchQueryParser(QueryParserConfig(default_roots=[Path("E:/docs")]))
        self.search = DocumentSearchService(self.catalog, self.parser, embeddings=self.index)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_indexes_chunks_and_skips_unindexed_documents(self) -> None:
        self.catalog.upsert_document(_document("E:\\docs\\acme.pdf", "[page 1]\nInvoice for consulting.\n" + "x" * 400 + "\n[page 2]\nInvoice total due."))
        self.catalog.upsert_document(_document("E:\\docs\\dinner.txt", "A recipe for soup."))
        self.catalog.upsert_document(_document("E:\\docs\\broken.pdf", "", status="text_unavailable"))
        self.assertEqual(len(self.index.pending()), 2)
        self.assertEqual(self.index.index_pending(), 2)
        self.assertGreaterEqual(self.index.count(), 3)
        self.assertEqual(self.index.pending(), [])
        self.assertTrue(all(text.startswith(DOCUMENT_PREFIX + "acme.pdf: ") or text.startswith(DOCUMENT_PREFIX + "dinner.txt: ") for call in self.embedder.calls for text in call))

        hits = self.index.search("bill for services")
        self.assertEqual(hits[0].path, "E:\\docs\\acme.pdf")
        self.assertIn(hits[0].page, {1, 2})
        self.assertEqual(len(hits), 2, "one hit per document")

    def test_changed_document_is_reindexed_and_removed_one_forgotten(self) -> None:
        self.catalog.upsert_document(_document("E:\\docs\\a.txt", "recipe one", content_hash="h1"))
        self.index.index_pending()
        before = self.index.vectors.keys()
        self.catalog.upsert_document(_document("E:\\docs\\a.txt", "recipe two", content_hash="h2"))
        self.assertEqual(len(self.index.pending()), 1)
        self.assertEqual(self.index.index_pending(), 1)
        after = self.index.vectors.keys()
        self.assertTrue(before.isdisjoint(after))
        self.assertEqual(len(after), 1)

        self.catalog.remove_paths(["E:\\docs\\a.txt"])
        self.assertEqual(self.index.index_pending(), 0)
        self.assertEqual(self.index.vectors.keys(), set())
        self.assertEqual(self.index.count(), 0)

    def test_embedder_outage_leaves_the_document_pending(self) -> None:
        self.catalog.upsert_document(_document("E:\\docs\\a.txt", "recipe"))
        self.embedder.fail = True
        self.assertEqual(self.index.index_pending(), 0)
        self.assertIn("Ollama is down", self.index.last_error or "")
        self.assertEqual(len(self.index.pending()), 1)
        self.embedder.fail = False
        self.assertEqual(self.index.index_pending(), 1)
        self.assertIsNone(self.index.last_error)

    def test_search_finds_a_document_by_meaning_and_cites_the_passage(self) -> None:
        self.catalog.upsert_document(_document("E:\\docs\\acme.pdf", "[page 1]\nInvoice for consulting services rendered."))
        self.catalog.upsert_document(_document("E:\\docs\\dinner.txt", "Cooking instructions for soup."))
        self.index.index_pending()

        results = self.search.search("bill for services", limit=5)
        self.assertEqual(results[0].record.path, "E:\\docs\\acme.pdf")
        self.assertTrue(any("matches by meaning" in reason for reason in results[0].reasons), results[0].reasons)
        self.assertEqual(results[0].location, "page 1")
        self.assertIn("Invoice for consulting", results[0].snippet or "")
        self.assertGreater(results[0].score, next(r.score for r in results if r.record.path.endswith("dinner.txt")))

    def test_lexical_only_when_the_index_is_off(self) -> None:
        self.catalog.upsert_document(_document("E:\\docs\\invoice.txt", "Invoice for consulting."))
        plain = DocumentSearchService(self.catalog, self.parser)
        results = plain.search("invoice", limit=5)
        self.assertEqual(results[0].record.name, "invoice.txt")
        self.assertIsNone(results[0].snippet)

        self.embedder.fail = True
        self.index.index_pending()
        results = self.search.search("invoice", limit=5)
        self.assertEqual(results[0].record.name, "invoice.txt", "an embedder outage does not break search")

    def test_config_reads_the_document_search_block(self) -> None:
        config = DocumentEmbeddingConfig.from_config({"models": {"tasks": {"embedding": "nomic-embed-text"}}, "document_search": {"embeddings": {"chunk_chars": 800}}})
        self.assertEqual(config.model, "nomic-embed-text")
        self.assertEqual(config.chunk_chars, 800)
        self.assertEqual(config.relevance(0.65), 0.5)


if __name__ == "__main__":
    unittest.main()
