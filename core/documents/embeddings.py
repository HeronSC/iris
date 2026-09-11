# File: core/documents/embeddings.py

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from core.knowledge.embeddings import DOCUMENT_PREFIX, QUERY_PREFIX, Embedder
from core.storage.sqlite_database import SQLiteDatabase
from core.storage.vector_table import VectorTable, extension_available, relevance

logger = logging.getLogger(__name__)

_PAGE_MARKER = re.compile(r"\[page (\d+)\]")


@dataclass(frozen=True)
class DocumentEmbeddingConfig:
    enabled: bool = True
    model: str = "nomic-embed-text"
    dimensions: int = 768
    batch_size: int = 16
    chunk_chars: int = 1200
    chunk_overlap: int = 150
    max_chunks_per_document: int = 40
    similarity_floor: float = 0.5
    similarity_ceiling: float = 0.8
    isolate_writes: bool = True
    write_timeout_seconds: float = 120.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DocumentEmbeddingConfig":
        documents = config.get("document_search") if isinstance(config.get("document_search"), dict) else {}
        section = documents.get("embeddings") if isinstance(documents.get("embeddings"), dict) else {}
        models = config.get("models") if isinstance(config.get("models"), dict) else {}
        routed = (models.get("tasks") or {}).get("embedding") if isinstance(models.get("tasks"), dict) else None
        return cls(
            enabled=bool(section.get("enabled", True)),
            model=str(section.get("model") or routed or cls.model),
            dimensions=int(section.get("dimensions", cls.dimensions)),
            batch_size=max(1, int(section.get("batch_size", cls.batch_size))),
            chunk_chars=max(200, int(section.get("chunk_chars", cls.chunk_chars))),
            chunk_overlap=max(0, int(section.get("chunk_overlap", cls.chunk_overlap))),
            max_chunks_per_document=max(1, int(section.get("max_chunks_per_document", cls.max_chunks_per_document))),
            similarity_floor=float(section.get("similarity_floor", cls.similarity_floor)),
            similarity_ceiling=float(section.get("similarity_ceiling", cls.similarity_ceiling)),
            isolate_writes=bool(section.get("isolate_writes", cls.isolate_writes)),
            write_timeout_seconds=float(section.get("write_timeout_seconds", cls.write_timeout_seconds)),
        )

    def relevance(self, similarity: float) -> float:
        return relevance(similarity, self.similarity_floor, self.similarity_ceiling)


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int
    end: int
    text: str
    page: int | None


@dataclass(frozen=True)
class DocumentHit:
    path: str
    similarity: float
    chunk_index: int
    start: int
    end: int
    page: int | None


def chunk_text(text: str, *, chunk_chars: int = 1200, overlap: int = 150, max_chunks: int = 40) -> list[Chunk]:
    body = text or ""
    length = len(body)
    if not body.strip():
        return []
    chunks: list[Chunk] = []
    start = 0
    while start < length and len(chunks) < max_chunks:
        end = min(length, start + chunk_chars)
        if end < length:
            window = body[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
            if cut > chunk_chars // 2:
                end = start + cut + 1
        piece = body[start:end]
        if piece.strip():
            markers = _PAGE_MARKER.findall(body[: max(start, 0) + 1] + piece[: min(len(piece), 12)])
            page = int(markers[-1]) if markers else None
            chunks.append(Chunk(index=len(chunks), start=start, end=end, text=piece.strip(), page=page))
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks


class DocumentEmbeddingIndex:
    name = "documents"

    def __init__(self, database: SQLiteDatabase, embedder: Embedder | None, config: DocumentEmbeddingConfig | None = None) -> None:
        self.database = database
        self.embedder = embedder
        self.config = config or DocumentEmbeddingConfig()
        self.last_error: str | None = None
        self.vectors = VectorTable(
            database,
            "document_chunks_vec",
            self.config.dimensions,
            model_stamp=f"{self.config.model}:{self.config.dimensions}",
            key_column="chunk_id",
            isolate_writes=self.config.isolate_writes,
            write_timeout_seconds=self.config.write_timeout_seconds,
        )
        if self.available:
            self.ensure_schema()

    @property
    def available(self) -> bool:
        if not self.config.enabled or self.embedder is None or not extension_available():
            return False
        return self.vectors.available


    def ensure_schema(self) -> None:
        with self.database.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    start INTEGER NOT NULL,
                    end INTEGER NOT NULL,
                    page INTEGER
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_document_chunks_path ON document_chunks(path, content_hash)")
            conn.commit()
        if self.vectors.ensure_schema():
            with self.database.connect() as conn:
                conn.execute("DELETE FROM document_chunks")
                conn.commit()


    def pending(self, limit: int | None = None) -> list[tuple[str, str, str, str]]:
        if not self.available:
            return []
        sql = (
            "SELECT d.path, d.name, d.content_hash, d.extracted_text FROM documents d "
            "WHERE d.content_status = 'indexed' AND d.extracted_text <> '' "
            "AND NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.path = d.path AND c.content_hash = d.content_hash) "
            "ORDER BY d.indexed_at"
        )
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.database.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]

    def stale_chunk_ids(self) -> list[int]:
        if not self.available:
            return []
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT c.id FROM document_chunks c LEFT JOIN documents d "
                "ON d.path = c.path AND d.content_hash = c.content_hash WHERE d.path IS NULL"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def index_pending(self, limit: int | None = None) -> int:
        if not self.available or self.embedder is None:
            return 0
        self._forget(self.stale_chunk_ids())
        indexed = 0
        for path, name, content_hash, text in self.pending(limit):
            chunks = chunk_text(
                text,
                chunk_chars=self.config.chunk_chars,
                overlap=self.config.chunk_overlap,
                max_chunks=self.config.max_chunks_per_document,
            )
            if not chunks:
                self._insert_chunks(path, content_hash, [])
                continue
            ids = self._insert_chunks(path, content_hash, chunks)
            try:
                rows: list[tuple[int, list[float]]] = []
                for start in range(0, len(chunks), self.config.batch_size):
                    batch = chunks[start : start + self.config.batch_size]
                    texts = [f"{DOCUMENT_PREFIX}{name}: {chunk.text}" for chunk in batch]
                    vectors = self.embedder.embed(texts, model=self.config.model)
                    if len(vectors) != len(batch):
                        raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(batch)} chunks")
                    rows.extend(zip(ids[start : start + len(batch)], vectors))
                self.vectors.store(rows)
            except Exception as error:
                self.last_error = str(error)
                logger.warning("Document embedding stopped at %s after %d documents: %s", path, indexed, error)
                self._forget(ids)
                return indexed
            indexed += 1
        self.last_error = None
        return indexed

    def _insert_chunks(self, path: str, content_hash: str, chunks: list[Chunk]) -> list[int]:
        with self.database.connect() as conn:
            existing = [int(row[0]) for row in conn.execute("SELECT id FROM document_chunks WHERE path = ?", (path,)).fetchall()]
            conn.execute("DELETE FROM document_chunks WHERE path = ?", (path,))
            ids: list[int] = []
            if not chunks:
                conn.execute(
                    "INSERT INTO document_chunks(path, content_hash, chunk_index, start, end, page) VALUES (?, ?, -1, 0, 0, NULL)",
                    (path, content_hash),
                )
            for chunk in chunks:
                cursor = conn.execute(
                    "INSERT INTO document_chunks(path, content_hash, chunk_index, start, end, page) VALUES (?, ?, ?, ?, ?, ?)",
                    (path, content_hash, chunk.index, chunk.start, chunk.end, chunk.page),
                )
                ids.append(int(cursor.lastrowid))
            conn.commit()
        if existing:
            try:
                self.vectors.delete(existing)
            except Exception as error:
                logger.warning("Could not drop old vectors for %s: %s", path, error)
        return ids

    def _forget(self, chunk_ids: list[int]) -> None:
        if not chunk_ids:
            return
        with self.database.connect() as conn:
            conn.executemany("DELETE FROM document_chunks WHERE id = ?", [(chunk_id,) for chunk_id in chunk_ids])
            conn.commit()
        try:
            self.vectors.delete(chunk_ids)
        except Exception as error:
            logger.warning("Could not delete %d stale vectors: %s", len(chunk_ids), error)


    def search(self, text: str, k: int = 50) -> list[DocumentHit]:
        if not self.available or self.embedder is None or not text.strip() or k <= 0:
            return []
        vectors = self.embedder.embed([QUERY_PREFIX + text.strip()], model=self.config.model)
        if not vectors:
            return []
        nearest = self.vectors.search(vectors[0], k)
        if not nearest:
            return []
        similarity = {chunk_id: score for chunk_id, score in nearest}
        placeholders = ", ".join("?" * len(similarity))
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT id, path, chunk_index, start, end, page FROM document_chunks WHERE id IN ({placeholders})",
                list(similarity.keys()),
            ).fetchall()
        best: dict[str, DocumentHit] = {}
        for row in rows:
            hit = DocumentHit(
                path=str(row[1]),
                similarity=similarity[int(row[0])],
                chunk_index=int(row[2]),
                start=int(row[3]),
                end=int(row[4]),
                page=int(row[5]) if row[5] is not None else None,
            )
            current = best.get(hit.path)
            if current is None or hit.similarity > current.similarity:
                best[hit.path] = hit
        return sorted(best.values(), key=lambda item: -item.similarity)

    def count(self) -> int:
        return self.vectors.count() if self.available else 0

    def status(self) -> dict[str, Any]:
        available = self.available
        return {
            "available": available,
            "enabled": self.config.enabled,
            "model": self.config.model,
            "dimensions": self.config.dimensions,
            "vectors": self.count() if available else 0,
            "pending": len(self.pending()) if available else 0,
            "last_error": self.last_error,
        }
