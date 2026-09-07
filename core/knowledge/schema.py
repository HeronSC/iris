from __future__ import annotations

from core.storage.sqlite_database import SQLiteDatabase


SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    -- Monotonic write order. created_at is only second-resolution and can be
    -- backdated when history is loaded, so it ties often and is not a stable
    -- sort on its own. This breaks those ties deterministically, and gives
    -- retrieval a cursor to page on.
    sequence INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL,
    source TEXT NOT NULL,
    source_ref TEXT,
    occurred_at TEXT,
    created_at TEXT NOT NULL,
    supersedes TEXT REFERENCES memories(id),
    superseded_by TEXT REFERENCES memories(id)
);

-- Retrieval filters in SQL before it scores anything in Python, so the indexes
-- carry the query shapes slice 3 will use. The lead index covers the ORDER BY
-- as well as the WHERE, which keeps SQLite off a temp b-tree at volume.
CREATE INDEX IF NOT EXISTS idx_memories_topic_kind
    ON memories(topic, kind, created_at DESC, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_memories_kind_status ON memories(kind, status);
CREATE INDEX IF NOT EXISTS idx_memories_occurred ON memories(occurred_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_supersedes ON memories(supersedes)
    WHERE supersedes IS NOT NULL;

-- Directed, typed edges between records. Evidence is not a separate table:
-- "supports" and "contradicts" are relations, and an evidence view is a query
-- over them. A second table would have duplicated these columns to answer the
-- same questions.
CREATE TABLE IF NOT EXISTS memory_links (
    id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    source_id TEXT NOT NULL REFERENCES memories(id),
    target_id TEXT NOT NULL REFERENCES memories(id),
    relation TEXT NOT NULL,
    weight REAL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_links_edge
    ON memory_links(source_id, target_id, relation);
CREATE INDEX IF NOT EXISTS idx_links_target ON memory_links(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_links_source ON memory_links(source_id, relation);
"""


def ensure_schema(database: SQLiteDatabase) -> None:
    """Create every knowledge table. Idempotent, and safe to call from any repository.

    Both repositories call this rather than owning a fragment each, so
    memory_links can carry real foreign keys to memories regardless of which
    one is constructed first.
    """
    with database.connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def next_sequence(conn: object, table: str) -> int:
    """The next write-order number for a table. Callers hold the transaction."""
    if table not in {"memories", "memory_links"}:
        raise ValueError(f"Unknown table: {table}")
    row = conn.execute(f"SELECT COALESCE(MAX(sequence), 0) + 1 FROM {table}").fetchone()  # type: ignore[attr-defined]
    return int(row[0])
