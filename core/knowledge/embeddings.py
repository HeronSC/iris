# File: core/knowledge/embeddings.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from core.knowledge.models import MemoryKind, MemoryRecord
from core.storage.sqlite_database import SQLiteDatabase
from core.storage.vector_table import VectorTable, extension_available, relevance

logger = logging.getLogger(__name__)

QUERY_PREFIX = "search_query: "
DOCUMENT_PREFIX = "search_document: "


class Embedder(Protocol):
    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        ...


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool = True
    model: str = "nomic-embed-text"
    dimensions: int = 768
    batch_size: int = 32
    skip_sources: tuple[str, ...] = ("bot:", "iris:shadow", "baseline:")
    similarity_floor: float = 0.5
    similarity_ceiling: float = 0.8
    refresh_seconds: float = 120.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "EmbeddingConfig":
        knowledge = config.get("knowledge") if isinstance(config.get("knowledge"), dict) else {}
        section = knowledge.get("embeddings") if isinstance(knowledge.get("embeddings"), dict) else {}
        skip_raw = section.get("skip_sources")
        skip = tuple(str(item) for item in skip_raw) if isinstance(skip_raw, list) else cls.skip_sources
        models = config.get("models") if isinstance(config.get("models"), dict) else {}
        routed = (models.get("tasks") or {}).get("embedding") if isinstance(models.get("tasks"), dict) else None
        return cls(
            enabled=bool(section.get("enabled", True)),
            model=str(section.get("model") or routed or cls.model),
            dimensions=int(section.get("dimensions", cls.dimensions)),
            batch_size=max(1, int(section.get("batch_size", cls.batch_size))),
            skip_sources=skip,
            similarity_floor=float(section.get("similarity_floor", cls.similarity_floor)),
            similarity_ceiling=float(section.get("similarity_ceiling", cls.similarity_ceiling)),
            refresh_seconds=float(section.get("refresh_seconds", cls.refresh_seconds)),
        )

    def relevance(self, similarity: float) -> float:
        return relevance(similarity, self.similarity_floor, self.similarity_ceiling)


def embedding_text(record: MemoryRecord) -> str:
    topic = record.topic.replace("/", " ").replace("-", " ").strip()
    return f"{topic}: {record.content}" if topic else record.content


class MemoryEmbeddingIndex:
    name = "memory"

    def __init__(self, database: SQLiteDatabase, embedder: Embedder | None, config: EmbeddingConfig | None = None) -> None:
        self.database = database
        self.embedder = embedder
        self.config = config or EmbeddingConfig()
        self.last_error: str | None = None
        self.vectors = VectorTable(
            database,
            "memories_vec",
            self.config.dimensions,
            model_stamp=f"{self.config.model}:{self.config.dimensions}",
            key_column="sequence",
        )
        if self.available:
            self.ensure_schema()


    @property
    def available(self) -> bool:
        if not self.config.enabled or self.embedder is None or not extension_available():
            return False
        return self.vectors.available


    def ensure_schema(self) -> None:
        self.vectors.ensure_schema()


    def should_embed(self, record: MemoryRecord) -> bool:
        return not any(record.source.startswith(prefix) for prefix in self.config.skip_sources)

    def pending(self, limit: int | None = None) -> list[tuple[int, str]]:
        if not self.available:
            return []
        clauses = ["1 = 1"]
        params: list[Any] = []
        for prefix in self.config.skip_sources:
            clauses.append("m.source NOT LIKE ? ESCAPE '\\'")
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(escaped + "%")
        sql = f"SELECT m.sequence, m.topic, m.content FROM memories m WHERE {' AND '.join(clauses)} ORDER BY m.sequence"
        with self.database.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        done = self.vectors.keys()
        pending = [
            (int(row[0]), embedding_text(MemoryRecord(kind=MemoryKind.FACT, topic=str(row[1]), content=str(row[2]))))
            for row in rows
            if int(row[0]) not in done
        ]
        return pending[: int(limit)] if limit is not None else pending

    def index_pending(self, limit: int | None = None) -> int:
        if not self.available or self.embedder is None:
            return 0
        todo = self.pending(limit)
        stored = 0
        batch = self.config.batch_size
        for start in range(0, len(todo), batch):
            chunk = todo[start : start + batch]
            try:
                vectors = self.embedder.embed([DOCUMENT_PREFIX + text for _, text in chunk], model=self.config.model)
            except Exception as error:
                self.last_error = str(error)
                logger.warning("Embedding stopped after %d records: %s", stored, error)
                return stored
            if len(vectors) != len(chunk):
                self.last_error = f"embedder returned {len(vectors)} vectors for {len(chunk)} texts"
                logger.warning(self.last_error)
                return stored
            try:
                self.vectors.store(list(zip((sequence for sequence, _ in chunk), vectors)))
            except Exception as error:
                self.last_error = str(error)
                logger.warning("Storing vectors stopped after %d records: %s", stored, error)
                return stored
            stored += len(chunk)
        self.last_error = None
        return stored


    def search(self, text: str, k: int) -> list[tuple[int, float]]:
        if not self.available or self.embedder is None or not text.strip() or k <= 0:
            return []
        vectors = self.embedder.embed([QUERY_PREFIX + text.strip()], model=self.config.model)
        if not vectors:
            return []
        return self.vectors.search(vectors[0], k)

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
