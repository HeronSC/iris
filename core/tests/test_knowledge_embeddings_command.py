# File: core/tests/test_knowledge_embeddings_command.py

from __future__ import annotations

import unittest
from typing import Any

from core.assistant.knowledge_commands import KnowledgeCommandHandler


class FakeIndex:
    def __init__(self, available: bool = True, pending: int = 2) -> None:
        self.available = available
        self.pending_count = pending
        self.indexed = 0

    def index_pending(self) -> int:
        stored = self.pending_count
        self.indexed += stored
        self.pending_count = 0
        return stored

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "enabled": True,
            "model": "nomic-embed-text",
            "dimensions": 768,
            "vectors": 10 + self.indexed,
            "pending": self.pending_count,
            "last_error": None,
        }


class EmbeddingsCommandTests(unittest.TestCase):
    def _handler(self, index: Any) -> tuple[KnowledgeCommandHandler, list[str]]:
        output: list[str] = []
        handler = KnowledgeCommandHandler(workflow=None, output=lambda text, role=None: output.append(text), embeddings=index)  # type: ignore[arg-type]
        return handler, output

    def test_status_reports_vectors_and_pending(self) -> None:
        handler, output = self._handler(FakeIndex())
        self.assertTrue(handler.handle("/knowledge embeddings", {}))
        self.assertIn("nomic-embed-text (768 dims)", output[-1])
        self.assertIn("10 vectors stored, 2 records waiting", output[-1])
        self.assertIn("/knowledge embeddings index", output[-1])

    def test_index_runs_a_pass(self) -> None:
        index = FakeIndex()
        handler, output = self._handler(index)
        self.assertTrue(handler.handle("/knowledge embeddings index", {}))
        self.assertEqual(output[0], "Embedded 2 records.")
        self.assertIn("12 vectors stored, 0 records waiting", output[-1])

    def test_unavailable_and_unconfigured(self) -> None:
        handler, output = self._handler(FakeIndex(available=False))
        handler.handle("/knowledge embeddings", {})
        self.assertIn("Embedding retrieval is off", output[-1])
        handler, output = self._handler(None)
        handler.handle("/knowledge embeddings", {})
        self.assertIn("not configured", output[-1])


if __name__ == "__main__":
    unittest.main()
