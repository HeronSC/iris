from __future__ import annotations

from core.storage.sqlite_database import SQLiteDatabase


SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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

-- Relevance-ordered candidate selection, so a strong match is found because it
-- matches rather than because it is recent. SQLite ships this; hand-rolling a
-- ranker instead left an older record unreachable however well it matched.
--
-- tokenchars '.' keeps decimals whole: "2.1x" and "1.3" are the content of an
-- observation, and the default tokenizer splits them.
--
-- An external-content table, kept in step by one insert trigger. That is all
-- that is needed because records are append-only and their content is
-- immutable: only status and superseded_by ever change, and neither is indexed.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    topic,
    data_json,
    content='memories',
    content_rowid='sequence',
    tokenize="unicode61 tokenchars '.'"
);

CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, topic, data_json)
    VALUES (new.sequence, new.content, new.topic, new.data_json);
END;
"""


def ensure_schema(database: SQLiteDatabase) -> None:
    """Create every knowledge table. Idempotent, and safe to call from any repository.

    Both repositories call this rather than owning a fragment each, so
    memory_links can carry real foreign keys to memories regardless of which
    one is constructed first.
    """
    with database.connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


#: Bumped when a change needs work doing to databases that already exist.
SCHEMA_VERSION = 2


def _migrate(conn: object) -> None:
    """Bring an existing database up to the current schema version.

    Version 2 added the search index. Records written before it need indexing
    once, and the emptiness of the index cannot be used to detect that: an
    external-content FTS table answers COUNT(*) from the content table, so it
    always looks full. Hence an explicit version rather than an inference.
    """
    row = conn.execute("SELECT value FROM knowledge_meta WHERE key = 'schema_version'").fetchone()  # type: ignore[attr-defined]
    current = int(row[0]) if row is not None else 1

    if current < 2:
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")  # type: ignore[attr-defined]

    if current != SCHEMA_VERSION:
        conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO knowledge_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )


def next_sequence(conn: object, table: str) -> int:
    """The next write-order number for a table. Callers hold the transaction."""
    if table not in {"memories", "memory_links"}:
        raise ValueError(f"Unknown table: {table}")
    row = conn.execute(f"SELECT COALESCE(MAX(sequence), 0) + 1 FROM {table}").fetchone()  # type: ignore[attr-defined]
    return int(row[0])
