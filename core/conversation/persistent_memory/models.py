from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool
    database_path: Path
    max_recent_messages: int
    max_retrieved_topics: int
    minimum_topic_score: float
    minimum_retrieval_score: float
    maximum_memory_tokens: int
    maximum_supporting_excerpts: int
    maximum_supporting_excerpts_per_topic: int
    maximum_excerpt_chars: int
    summary_update_message_count: int
    include_current_topic: bool
    diagnostics: bool
    semantic_weight: float
    current_topic_bonus: float
    recency_bonus_max: float

    @staticmethod
    def from_config(config: dict[str, Any]) -> MemoryConfig:
        memory_cfg = config.get("memory", {}) if isinstance(config, dict) else {}
        if not isinstance(memory_cfg, dict):
            memory_cfg = {}

        db_path = memory_cfg.get("database_path")
        if not str(db_path or "").strip():
            data_root = Path(config.get("memory_path", Path.cwd())).expanduser().parent
            db_path = data_root / "Memory" / "conversations.db"
        db_path = Path(str(db_path)).expanduser()
        if not db_path.is_absolute():
            db_path = (Path.cwd() / db_path).resolve()

        return MemoryConfig(
            enabled=bool(memory_cfg.get("enabled", True)),
            database_path=db_path,
            max_recent_messages=max(1, int(memory_cfg.get("max_recent_messages", 12))),
            max_retrieved_topics=max(1, int(memory_cfg.get("max_retrieved_topics", 5))),
            minimum_topic_score=float(memory_cfg.get("minimum_topic_score", 0.68)),
            minimum_retrieval_score=float(memory_cfg.get("minimum_retrieval_score", 0.35)),
            maximum_memory_tokens=max(200, int(memory_cfg.get("maximum_memory_tokens", 2500))),
            maximum_supporting_excerpts=max(0, int(memory_cfg.get("maximum_supporting_excerpts", 6))),
            maximum_supporting_excerpts_per_topic=max(0, int(memory_cfg.get("maximum_supporting_excerpts_per_topic", 3))),
            maximum_excerpt_chars=max(80, int(memory_cfg.get("maximum_excerpt_chars", 280))),
            summary_update_message_count=max(1, int(memory_cfg.get("summary_update_message_count", 6))),
            include_current_topic=bool(memory_cfg.get("include_current_topic", True)),
            diagnostics=bool(memory_cfg.get("diagnostics", False)),
            semantic_weight=float(memory_cfg.get("semantic_weight", 0.75)),
            current_topic_bonus=float(memory_cfg.get("current_topic_bonus", 0.12)),
            recency_bonus_max=float(memory_cfg.get("recency_bonus_max", 0.13)),
        )


@dataclass(frozen=True)
class TopicRecord:
    id: int
    name: str
    summary: str
    created_at: str
    updated_at: str
    last_active_at: str
    status: str


@dataclass(frozen=True)
class TopicCandidate:
    topic: TopicRecord
    semantic_similarity: float
    current_topic_bonus: float
    recency_bonus: float
    final_score: float


@dataclass(frozen=True)
class PreparedMemoryContext:
    conversation_id: str
    topic_id: int
    topic_name: str
    user_message_id: int
    user_message: str
    expected_version: int
    related_topic_ids: list[int]
    context_block: str
    diagnostics: dict[str, Any]


def _json_object_or_empty(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list_of_objects(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _topic_from_row(row: Any) -> TopicRecord:
    return TopicRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        summary=str(row["summary"] or ""),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_active_at=str(row["last_active_at"]),
        status=str(row["status"]),
    )


def _topic_basis(topic: TopicRecord) -> str:
    if topic.summary.strip():
        return f"{topic.name}\n{topic.summary}"
    return topic.name


def _deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True))


def _trim_change_log(change_log: list[dict[str, Any]], limit: int = 300) -> list[dict[str, Any]]:
    if len(change_log) <= limit:
        return change_log
    return change_log[-limit:]


def diagnostics_to_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("Memory retrieval:")
    selected = payload.get("selected_topics", []) if isinstance(payload, dict) else []
    for item in selected:
        if not isinstance(item, dict):
            continue
        name = str(item.get("topic", "Unknown"))
        score = float(item.get("final_score", 0.0))
        included = bool(item.get("included", False))
        status = "included" if included else "excluded below threshold"
        lines.append(f"- {name}: {score:.2f}, {status}")
    return "\n".join(lines)
