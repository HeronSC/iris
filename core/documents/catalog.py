from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

from core.documents.models import FileRecord, FileSearchQuery
from core.storage.sqlite_database import SQLiteDatabase


class DocumentCatalog:
    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db
        self.fts_enabled = False
        self._initialize()

    def _initialize(self) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    modified_at TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content_status TEXT NOT NULL,
                    extracted_text TEXT NOT NULL,
                    extractor TEXT NOT NULL,
                    error TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_extension ON documents(extension)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_modified_at ON documents(modified_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(path)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                        path,
                        name,
                        extracted_text
                    )
                    """
                )
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False

    def upsert_document(self, payload: dict[str, Any]) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    id, path, name, extension, size, created_at, modified_at, indexed_at,
                    content_hash, content_status, extracted_text, extractor, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    id = excluded.id,
                    name = excluded.name,
                    extension = excluded.extension,
                    size = excluded.size,
                    created_at = excluded.created_at,
                    modified_at = excluded.modified_at,
                    indexed_at = excluded.indexed_at,
                    content_hash = excluded.content_hash,
                    content_status = excluded.content_status,
                    extracted_text = excluded.extracted_text,
                    extractor = excluded.extractor,
                    error = excluded.error
                """,
                (
                    payload["id"],
                    payload["path"],
                    payload["name"],
                    payload["extension"],
                    payload["size"],
                    payload["created_at"],
                    payload["modified_at"],
                    payload["indexed_at"],
                    payload["content_hash"],
                    payload["content_status"],
                    payload["extracted_text"],
                    payload["extractor"],
                    payload.get("error"),
                ),
            )
            if self.fts_enabled:
                connection.execute("DELETE FROM documents_fts WHERE path = ?", (payload["path"],))
                connection.execute(
                    "INSERT INTO documents_fts(path, name, extracted_text) VALUES (?, ?, ?)",
                    (payload["path"], payload["name"], payload["extracted_text"]),
                )

    def get_by_path(self, path: str) -> FileRecord | None:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE path = ?", (path,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def get_by_id(self, record_id: str) -> FileRecord | None:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id = ?", (record_id,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def remove_paths(
        self,
        paths: list[str],
        progress_callback: Callable[[int, int], None] | None = None,
        progress_interval: int = 5000,
    ) -> int:
        if not paths:
            return 0
        total = len(paths)
        interval = max(1, progress_interval)
        with self.db.connect() as connection:
            count = 0
            for path in paths:
                connection.execute("DELETE FROM documents WHERE path = ?", (path,))
                if self.fts_enabled:
                    connection.execute("DELETE FROM documents_fts WHERE path = ?", (path,))
                count = count + 1
                if progress_callback is not None and (count % interval == 0 or count == total):
                    progress_callback(count, total)
            return count

    def list_paths_under_roots(
        self,
        roots: list[Path],
        progress_callback: Callable[[int, int], None] | None = None,
        progress_interval: int = 5000,
    ) -> list[str]:
        if not roots:
            return []
        normalized_roots = [self._normalize_path_text(str(root.resolve())) for root in roots]
        kept: list[str] = []
        interval = max(1, progress_interval)
        processed = 0
        with self.db.connect() as connection:
            total_row = connection.execute("SELECT COUNT(*) AS value FROM documents").fetchone()
            total = int(total_row["value"]) if total_row is not None else 0
            rows = connection.execute("SELECT path FROM documents")
            for row in rows:
                processed = processed + 1
                path_text = str(row["path"])
                normalized_path = self._normalize_path_text(path_text)
                if any(
                    normalized_path == root or normalized_path.startswith(root + os.sep)
                    for root in normalized_roots
                ):
                    kept.append(path_text)
                if progress_callback is not None and (processed % interval == 0 or processed == total):
                    progress_callback(processed, total)
        return kept

    def count_documents(self) -> int:
        with self.db.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS value FROM documents").fetchone()
        if row is None:
            return 0
        return int(row["value"])

    def prune_missing_paths_under_roots(
        self,
        roots: list[Path],
        seen_paths: set[str],
        seen_progress_callback: Callable[[int, int], None] | None = None,
        delete_progress_callback: Callable[[int, int], None] | None = None,
        progress_interval: int = 5000,
        batch_size: int = 1000,
    ) -> int:
        if not roots:
            return 0

        normalized_roots = [root.resolve() for root in roots]
        interval = max(1, progress_interval)
        chunk_size = max(100, batch_size)
        total_seen = len(seen_paths)

        where_parts: list[str] = []
        where_params: list[str] = []
        for root in normalized_roots:
            root_text = str(root)
            root_prefix = root_text.rstrip("\\/") + os.sep
            where_parts.append("(path = ? OR path LIKE ?)")
            where_params.append(root_text)
            where_params.append(root_prefix + "%")

        where_sql = " OR ".join(where_parts)

        with self.db.connect() as connection:
            connection.execute("CREATE TEMP TABLE IF NOT EXISTS temp_seen_paths(path TEXT PRIMARY KEY)")
            connection.execute("CREATE TEMP TABLE IF NOT EXISTS temp_stale_paths(path TEXT PRIMARY KEY)")
            connection.execute("DELETE FROM temp_seen_paths")
            connection.execute("DELETE FROM temp_stale_paths")

            if total_seen > 0:
                seen_items = sorted(seen_paths)
                inserted = 0
                while inserted < total_seen:
                    upper = min(inserted + chunk_size, total_seen)
                    slice_items = seen_items[inserted:upper]
                    connection.executemany(
                        "INSERT OR IGNORE INTO temp_seen_paths(path) VALUES (?)",
                        ((item,) for item in slice_items),
                    )
                    inserted = upper
                    if seen_progress_callback is not None and (inserted % interval == 0 or inserted == total_seen):
                        seen_progress_callback(inserted, total_seen)

            connection.execute(
                """
                INSERT INTO temp_stale_paths(path)
                SELECT d.path
                FROM documents d
                WHERE (
                """
                + where_sql
                + """
                )
                AND NOT EXISTS (SELECT 1 FROM temp_seen_paths s WHERE s.path = d.path)
                """,
                tuple(where_params),
            )

            stale_row = connection.execute("SELECT COUNT(*) AS value FROM temp_stale_paths").fetchone()
            total_stale = int(stale_row["value"]) if stale_row is not None else 0
            if delete_progress_callback is not None:
                delete_progress_callback(0, total_stale)

            if total_stale == 0:
                return 0

            deleted = 0
            while deleted < total_stale:
                rows = connection.execute("SELECT path FROM temp_stale_paths LIMIT ?", (chunk_size,)).fetchall()
                if not rows:
                    break

                batch_paths = [str(row["path"]) for row in rows]
                connection.executemany("DELETE FROM documents WHERE path = ?", ((path,) for path in batch_paths))
                if self.fts_enabled:
                    connection.executemany("DELETE FROM documents_fts WHERE path = ?", ((path,) for path in batch_paths))
                connection.executemany("DELETE FROM temp_stale_paths WHERE path = ?", ((path,) for path in batch_paths))

                deleted = deleted + len(batch_paths)
                if delete_progress_callback is not None and (deleted % interval == 0 or deleted >= total_stale):
                    delete_progress_callback(min(deleted, total_stale), total_stale)

            return min(deleted, total_stale)

    def set_scan_state(self, key: str, value: str) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_scan_state(self, key: str) -> str | None:
        with self.db.connect() as connection:
            row = connection.execute("SELECT value FROM scan_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def log_scan_error(self, path: str, error: str) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO scan_errors(path, error, created_at) VALUES (?, ?, ?)",
                (path, error, self._utc_now_iso()),
            )

    def list_scan_errors(self, limit: int = 20) -> list[dict[str, str]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT path, error, created_at FROM scan_errors ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "path": str(row["path"]),
                "error": str(row["error"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def search_candidates(self, query: FileSearchQuery, limit: int = 200) -> list[FileRecord]:
        where_parts, params = self._build_where_parts(query, table_alias="d")
        where_sql = " WHERE " + " AND ".join(where_parts) if where_parts else ""

        with self.db.connect() as connection:
            if query.text_terms and self.fts_enabled:
                safe_terms: list[str] = []
                for term in query.text_terms:
                    escaped = term.replace('"', '""')
                    safe_terms.append(f'"{escaped}"')
                fts_query = " OR ".join(safe_terms)
                join_sql = "SELECT d.* FROM documents d JOIN documents_fts ON d.path = documents_fts.path"
                clause = where_sql + (" AND " if where_sql else " WHERE ") + "documents_fts MATCH ?"
                try:
                    rows = connection.execute(
                        join_sql + clause + " ORDER BY d.modified_at DESC LIMIT ?",
                        (*params, fts_query, limit),
                    ).fetchall()
                    return [self._row_to_record(row) for row in rows]
                except sqlite3.OperationalError:
                    # Fallback to deterministic Python filtering if FTS query parsing fails.
                    pass

            fallback_where_parts, fallback_params = self._build_where_parts(query, table_alias=None)
            fallback_where_sql = " WHERE " + " AND ".join(fallback_where_parts) if fallback_where_parts else ""
            rows = connection.execute(
                "SELECT * FROM documents" + fallback_where_sql + " ORDER BY modified_at DESC",
                tuple(fallback_params),
            ).fetchall()

        records = [self._row_to_record(row) for row in rows]
        if not query.text_terms:
            return records[:limit]

        terms = [term.lower() for term in query.text_terms]
        filtered: list[FileRecord] = []
        for record in records:
            haystack = f"{record.name}\n{record.path}\n{record.extracted_text}".lower()
            if any(term in haystack for term in terms):
                filtered.append(record)
        return filtered[:limit]

    def recent_documents(self, limit: int = 5) -> list[FileRecord]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM documents ORDER BY modified_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def _row_to_record(self, row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=str(row["id"]),
            path=str(row["path"]),
            name=str(row["name"]),
            extension=str(row["extension"]),
            size=int(row["size"]),
            created_at=str(row["created_at"]),
            modified_at=str(row["modified_at"]),
            indexed_at=str(row["indexed_at"]),
            content_hash=str(row["content_hash"]),
            content_status=str(row["content_status"]),
            extracted_text=str(row["extracted_text"]),
            extractor=str(row["extractor"]),
            error=str(row["error"]) if row["error"] is not None else None,
        )

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _is_under_root(self, path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _normalize_path_text(self, value: str) -> str:
        return os.path.normcase(os.path.normpath(value))

    def _build_where_parts(self, query: FileSearchQuery, table_alias: str | None) -> tuple[list[str], list[Any]]:
        where_parts: list[str] = []
        params: list[Any] = []

        prefix = f"{table_alias}." if table_alias else ""

        if query.extensions:
            placeholders = ",".join("?" for _ in query.extensions)
            where_parts.append(f"{prefix}extension IN ({placeholders})")
            params.extend(query.extensions)

        if query.created_after is not None:
            where_parts.append(f"{prefix}created_at >= ?")
            params.append(query.created_after.isoformat())
        if query.created_before is not None:
            where_parts.append(f"{prefix}created_at <= ?")
            params.append(query.created_before.isoformat())
        if query.modified_after is not None:
            where_parts.append(f"{prefix}modified_at >= ?")
            params.append(query.modified_after.isoformat())
        if query.modified_before is not None:
            where_parts.append(f"{prefix}modified_at <= ?")
            params.append(query.modified_before.isoformat())

        location_parts: list[str] = []
        for location in query.locations:
            resolved = str(location.resolve())
            root_prefix = resolved.rstrip("\\/") + os.sep
            field = f"{prefix}path"
            location_parts.append(f"({field} = ? OR {field} LIKE ?)")
            params.append(resolved)
            params.append(root_prefix + "%")
        if location_parts:
            where_parts.append("(" + " OR ".join(location_parts) + ")")

        return where_parts, params

