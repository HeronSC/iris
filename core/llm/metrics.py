# File: core/llm/metrics.py

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.storage.sqlite_database import SQLiteDatabase

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    task TEXT,
    requested_model TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_duration_ms REAL NOT NULL DEFAULT 0,
    load_duration_ms REAL NOT NULL DEFAULT 0,
    wall_ms REAL NOT NULL DEFAULT 0,
    tool_calls TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL,
    error TEXT,
    fallback INTEGER NOT NULL DEFAULT 0,
    request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_requests_created ON llm_requests(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_requests_task_model ON llm_requests(task, model);
"""


@dataclass(frozen=True)
class RequestMetric:
    task: str | None
    requested_model: str
    model: str
    outcome: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_duration_ms: float = 0.0
    load_duration_ms: float = 0.0
    wall_ms: float = 0.0
    tool_calls: tuple[str, ...] = ()
    error: str | None = None
    fallback: bool = False
    request_id: str | None = None


class RequestMetricsStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        with self.database.connect() as conn:
            conn.executescript(SCHEMA)

    def record(self, metric: RequestMetric) -> None:
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_requests (
                    created_at, task, requested_model, model, prompt_tokens, completion_tokens,
                    total_duration_ms, load_duration_ms, wall_ms, tool_calls, outcome, error, fallback, request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    metric.task,
                    metric.requested_model,
                    metric.model,
                    int(metric.prompt_tokens),
                    int(metric.completion_tokens),
                    float(metric.total_duration_ms),
                    float(metric.load_duration_ms),
                    float(metric.wall_ms),
                    json.dumps(list(metric.tool_calls)),
                    metric.outcome,
                    metric.error,
                    1 if metric.fallback else 0,
                    metric.request_id,
                ),
            )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_requests ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def summary(self, hours: float | None = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...] = ()
        if hours is not None:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="milliseconds")
            where = "WHERE created_at >= ?"
            params = (since,)
        with self.database.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(task, '') AS task,
                    model,
                    COUNT(*) AS calls,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    AVG(wall_ms) AS avg_wall_ms,
                    MAX(wall_ms) AS max_wall_ms,
                    SUM(CASE WHEN outcome <> 'ok' THEN 1 ELSE 0 END) AS errors,
                    SUM(fallback) AS fallbacks
                FROM llm_requests
                {where}
                GROUP BY task, model
                ORDER BY calls DESC, task, model
                """,
                params,
            ).fetchall()
        return [
            {
                "task": row[0],
                "model": row[1],
                "calls": int(row[2] or 0),
                "prompt_tokens": int(row[3] or 0),
                "completion_tokens": int(row[4] or 0),
                "avg_wall_ms": float(row[5] or 0.0),
                "max_wall_ms": float(row[6] or 0.0),
                "errors": int(row[7] or 0),
                "fallbacks": int(row[8] or 0),
            }
            for row in rows
        ]

    def count(self) -> int:
        with self.database.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM llm_requests").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        keys = [
            "id", "created_at", "task", "requested_model", "model", "prompt_tokens", "completion_tokens",
            "total_duration_ms", "load_duration_ms", "wall_ms", "tool_calls", "outcome", "error", "fallback", "request_id",
        ]
        record = dict(zip(keys, row))
        try:
            record["tool_calls"] = json.loads(record.get("tool_calls") or "[]")
        except (TypeError, ValueError):
            record["tool_calls"] = []
        record["fallback"] = bool(record.get("fallback"))
        return record
