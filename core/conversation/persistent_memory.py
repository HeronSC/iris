from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from core.storage.sqlite_database import SQLiteDatabase


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


class ConversationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def upsert_conversation(self, conversation_id: str, title: str | None = None) -> None:
        now = _utc_now()
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO conversations (id, title, created_at, updated_at, is_archived, current_topic_id) VALUES (?, ?, ?, ?, 0, NULL)",
                    (conversation_id, title or "Conversation", now, now),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET title = COALESCE(?, title), updated_at = ? WHERE id = ?",
                    (title, now, conversation_id),
                )
            conn.commit()

    def set_current_topic(self, conversation_id: str, topic_id: int) -> None:
        now = _utc_now()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE conversations SET current_topic_id = ?, updated_at = ? WHERE id = ?",
                (topic_id, now, conversation_id),
            )
            conn.commit()

    def get_current_topic_id(self, conversation_id: str) -> int | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT current_topic_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        value = row["current_topic_id"]
        if value is None:
            return None
        return int(value)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at, is_archived FROM conversations ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


class TopicRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def create_topic(self, name: str, status: str = "active") -> TopicRecord:
        now = _utc_now()
        with self.database.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO topics (name, summary, state_json, change_log_json, created_at, updated_at, last_active_at, embedding, status) VALUES (?, '', '{}', '[]', ?, ?, ?, NULL, ?)",
                (name, now, now, now, status),
            )
            conn.commit()
            topic_id = int(cursor.lastrowid)
        return self.get_topic(topic_id)

    def get_topic(self, topic_id: int) -> TopicRecord:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id, name, summary, created_at, updated_at, last_active_at, status FROM topics WHERE id = ?",
                (topic_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Topic not found: {topic_id}")
        return _topic_from_row(row)

    def find_topic(self, name_or_id: str) -> TopicRecord | None:
        needle = str(name_or_id).strip()
        if not needle:
            return None
        with self.database.connect() as conn:
            row = None
            if needle.isdigit():
                row = conn.execute(
                    "SELECT id, name, summary, created_at, updated_at, last_active_at, status FROM topics WHERE id = ?",
                    (int(needle),),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT id, name, summary, created_at, updated_at, last_active_at, status FROM topics WHERE lower(name) = lower(?) ORDER BY last_active_at DESC LIMIT 1",
                    (needle,),
                ).fetchone()
        if row is None:
            return None
        return _topic_from_row(row)

    def list_recent(self, limit: int = 20) -> list[TopicRecord]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, summary, created_at, updated_at, last_active_at, status FROM topics ORDER BY last_active_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_topic_from_row(row) for row in rows]

    def list_candidates(self) -> list[TopicRecord]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, summary, created_at, updated_at, last_active_at, status FROM topics WHERE status != 'archived' ORDER BY last_active_at DESC",
            ).fetchall()
        return [_topic_from_row(row) for row in rows]

    def update_summary(self, topic_id: int, summary: str) -> None:
        now = _utc_now()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE topics SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, now, topic_id),
            )
            conn.commit()

    def get_state(self, topic_id: int) -> dict[str, Any]:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM topics WHERE id = ?",
                (topic_id,),
            ).fetchone()
        if row is None:
            return {}
        return _json_object_or_empty(row["state_json"])

    def get_change_log(self, topic_id: int) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT change_log_json FROM topics WHERE id = ?",
                (topic_id,),
            ).fetchone()
        if row is None:
            return []
        return _json_list_of_objects(row["change_log_json"])

    def update_state(self, topic_id: int, state: dict[str, Any], change_log: list[dict[str, Any]], summary: str) -> None:
        now = _utc_now()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE topics SET state_json = ?, change_log_json = ?, summary = ?, updated_at = ? WHERE id = ?",
                (json.dumps(state, ensure_ascii=True), json.dumps(change_log, ensure_ascii=True), summary, now, topic_id),
            )
            conn.commit()

    def apply_operations(
        self,
        topic_id: int,
        operations: list[dict[str, Any]],
        *,
        expected_version: int | None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        topic = self.get_topic(topic_id)
        current_state = _normalize_topic_state(self.get_state(topic_id), topic_id=topic_id, topic_name=topic.name)
        change_log = self.get_change_log(topic_id)
        current_version = int(current_state.get("version", 1))
        now = _utc_now()

        if expected_version is not None and expected_version != current_version:
            rejected_entry = {
                "type": "patch_rejected",
                "at": now,
                "reason": "version_mismatch",
                "expected_version": expected_version,
                "actual_version": current_version,
                "operations": [item for item in operations if isinstance(item, dict)][:100],
            }
            change_log.append(rejected_entry)
            self.update_state(topic_id, current_state, _trim_change_log(change_log), _summary_from_topic_state(current_state))
            return {
                "applied": False,
                "reason": "version_mismatch",
                "version": current_version,
                "applied_operations": [],
                "rejected_operations": [rejected_entry],
            }

        sanitized_operations = [item for item in operations if isinstance(item, dict)][:100]
        policy = _build_patch_policy(current_state, scope)
        planned_restore_targets = _collect_restore_targets(current_state, sanitized_operations)
        validated_operations: list[dict[str, Any]] = []
        rejected_operations: list[dict[str, Any]] = []
        for index, operation in enumerate(sanitized_operations):
            allowed, reason = _validate_operation_against_policy(current_state, operation, policy, planned_restore_targets)
            if not allowed:
                rejected_operations.append({"index": index, "op": operation.get("op"), "reason": reason})
                continue
            validated_operations.append(operation)

        before_state = _deep_copy_json(current_state)
        applied_operations: list[dict[str, Any]] = []

        for index, operation in enumerate(validated_operations):
            accepted, reason = _apply_topic_operation(current_state, operation)
            if accepted:
                applied_operations.append({"index": index, "op": operation.get("op")})
            else:
                rejected_operations.append({"index": index, "op": operation.get("op"), "reason": reason})

        if applied_operations:
            current_state["version"] = current_version + 1
            current_state["updated_at"] = now
            change_log.append(
                {
                    "type": "patch_apply",
                    "at": now,
                    "version_before": current_version,
                    "version_after": current_state["version"],
                    "expected_version": expected_version,
                    "scope": policy,
                    "source": source or {},
                    "applied": applied_operations,
                    "rejected": rejected_operations,
                    "before_state": before_state,
                }
            )
            self.update_state(topic_id, current_state, _trim_change_log(change_log), _summary_from_topic_state(current_state))
            return {
                "applied": True,
                "version": int(current_state.get("version", current_version + 1)),
                "applied_operations": applied_operations,
                "rejected_operations": rejected_operations,
            }

        if rejected_operations:
            change_log.append(
                {
                    "type": "patch_rejected",
                    "at": now,
                    "reason": "validation_failed",
                    "expected_version": expected_version,
                    "actual_version": current_version,
                    "scope": policy,
                    "rejected": rejected_operations,
                }
            )
            self.update_state(topic_id, current_state, _trim_change_log(change_log), _summary_from_topic_state(current_state))
        return {
            "applied": False,
            "reason": "no_operations_applied",
            "version": current_version,
            "applied_operations": [],
            "rejected_operations": rejected_operations,
        }

    def undo_last_change(self, topic_id: int) -> tuple[bool, str]:
        topic = self.get_topic(topic_id)
        state = _normalize_topic_state(self.get_state(topic_id), topic_id=topic_id, topic_name=topic.name)
        change_log = self.get_change_log(topic_id)
        now = _utc_now()

        for index in range(len(change_log) - 1, -1, -1):
            entry = change_log[index]
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "")).strip() != "patch_apply":
                continue
            previous_state = entry.get("before_state")
            if not isinstance(previous_state, dict):
                continue
            restored = _normalize_topic_state(previous_state, topic_id=topic_id, topic_name=topic.name)
            restored["updated_at"] = now
            change_log.append(
                {
                    "type": "undo",
                    "at": now,
                    "reverted_change_index": index,
                    "version_after": restored.get("version"),
                }
            )
            self.update_state(topic_id, restored, _trim_change_log(change_log), _summary_from_topic_state(restored))
            return True, "Undo applied."

        return False, "No patch update available to undo."

    def touch_topic(self, topic_id: int) -> None:
        now = _utc_now()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE topics SET last_active_at = ?, updated_at = ?, status = 'active' WHERE id = ?",
                (now, now, topic_id),
            )
            conn.commit()


class MessageRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def add_message(self, conversation_id: str, topic_id: int, role: str, content: str) -> int:
        now = _utc_now()
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) AS max_seq FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            next_seq = int(row["max_seq"]) + 1 if row is not None else 1
            cursor = conn.execute(
                "INSERT INTO messages (conversation_id, topic_id, role, content, created_at, sequence_number) VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, topic_id, role, content, now, next_seq),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_recent_conversation_messages(self, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id, conversation_id, topic_id, role, content, created_at, sequence_number FROM messages WHERE conversation_id = ? ORDER BY sequence_number DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        ordered = [dict(row) for row in rows]
        ordered.reverse()
        return ordered

    def get_recent_topic_messages(self, topic_id: int, limit: int) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id, conversation_id, topic_id, role, content, created_at, sequence_number FROM messages WHERE topic_id = ? ORDER BY created_at DESC LIMIT ?",
                (topic_id, limit),
            ).fetchall()
        ordered = [dict(row) for row in rows]
        ordered.reverse()
        return ordered

    def get_topic_messages(self, topic_id: int) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT id, conversation_id, topic_id, role, content, created_at, sequence_number FROM messages WHERE topic_id = ? ORDER BY created_at ASC, id ASC",
                (topic_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_topic_messages(self, topic_id: int) -> int:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
        return int(row["c"]) if row is not None else 0


class TopicClassifier:
    def __init__(self, config: MemoryConfig) -> None:
        self.config = config

    def rank_topics(self, user_message: str, topics: list[TopicRecord], current_topic_id: int | None = None) -> list[TopicCandidate]:
        candidates: list[TopicCandidate] = []
        for topic in topics:
            basis = _topic_basis(topic)
            semantic_similarity = _semantic_similarity(user_message, basis)
            current_topic_bonus = self.config.current_topic_bonus if current_topic_id is not None and topic.id == current_topic_id else 0.0
            recency_bonus = self._recency_bonus(topic.last_active_at)
            final_score = (semantic_similarity * self.config.semantic_weight) + current_topic_bonus + recency_bonus
            candidates.append(
                TopicCandidate(
                    topic=topic,
                    semantic_similarity=semantic_similarity,
                    current_topic_bonus=current_topic_bonus,
                    recency_bonus=recency_bonus,
                    final_score=final_score,
                )
            )
        candidates.sort(key=lambda item: item.final_score, reverse=True)
        return candidates

    def classify(self, user_message: str, topics: list[TopicRecord], current_topic_id: int | None = None) -> tuple[TopicCandidate | None, list[TopicCandidate]]:
        candidates = self.rank_topics(user_message, topics, current_topic_id=current_topic_id)
        if not candidates:
            return None, []
        if current_topic_id is not None and (_looks_like_topic_mutation(user_message) or _looks_like_topic_enrichment(user_message)):
            for candidate in candidates:
                if candidate.topic.id == current_topic_id:
                    return candidate, candidates
        best = candidates[0]
        if best.final_score < self.config.minimum_topic_score:
            fallback_min_score = max(0.18, self.config.minimum_retrieval_score - 0.10)
            if _is_anaphoric_query(user_message) and best.final_score >= fallback_min_score:
                return best, candidates
            return None, candidates
        return best, candidates

    def _recency_bonus(self, iso_timestamp: str) -> float:
        if not iso_timestamp:
            return 0.0
        try:
            stamp = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        now = datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        delta_days = max(0.0, (now - stamp).total_seconds() / 86400.0)
        return self.config.recency_bonus_max * (1.0 / (1.0 + delta_days))


class TopicSummaryService:
    def __init__(self, config: MemoryConfig, message_repository: MessageRepository, topic_repository: TopicRepository) -> None:
        self.config = config
        self.message_repository = message_repository
        self.topic_repository = topic_repository

    def maybe_update_summary(self, topic: TopicRecord) -> None:
        message_count = self.message_repository.count_topic_messages(topic.id)
        if message_count <= 0:
            return
        # Seed a summary early so retrieval has meaningful text after the first exchange.
        if not topic.summary.strip() and message_count >= 2:
            messages = self.message_repository.get_recent_topic_messages(topic.id, limit=40)
            summary = self._build_summary(topic.name, messages)
            self.topic_repository.update_summary(topic.id, summary)
            return
        if message_count % self.config.summary_update_message_count != 0:
            return
        messages = self.message_repository.get_recent_topic_messages(topic.id, limit=40)
        summary = self._build_summary(topic.name, messages)
        self.topic_repository.update_summary(topic.id, summary)

    def _build_summary(self, topic_name: str, messages: list[dict[str, Any]]) -> str:
        user_messages = [str(item.get("content", "")).strip() for item in messages if str(item.get("role", "")) == "user"]
        assistant_messages = [str(item.get("content", "")).strip() for item in messages if str(item.get("role", "")) == "assistant"]

        goals = _dedupe_keep_order([item for item in user_messages[:3] if item])
        requirements = _extract_requirements(user_messages + assistant_messages)
        spec_facts = _extract_spec_facts(user_messages + assistant_messages)
        decisions = _extract_decisions(assistant_messages)
        recommendation_highlights = _extract_recommendation_highlights(assistant_messages)
        recommended_models = _extract_model_mentions(assistant_messages)
        open_questions = _extract_open_questions(user_messages)
        entities = _extract_entities(user_messages + assistant_messages)

        lines: list[str] = []
        lines.append(f"Topic: {topic_name}")
        if goals:
            lines.append("Goal:")
            lines.append(f"- {goals[0][:220]}")
        if requirements:
            lines.append("Requirements:")
            for item in requirements[:6]:
                lines.append(f"- {item[:180]}")
        elif spec_facts:
            lines.append("Requirements:")
            for item in spec_facts[:6]:
                lines.append(f"- {item[:180]}")
        if recommended_models:
            lines.append("Recommended models:")
            for item in recommended_models[:6]:
                lines.append(f"- {item[:180]}")
        if decisions:
            lines.append("Previously discussed:")
            for item in decisions[:6]:
                lines.append(f"- {item[:180]}")
        if recommendation_highlights:
            lines.append("Assistant highlights:")
            for item in recommendation_highlights[:6]:
                lines.append(f"- {item[:180]}")
        if entities:
            lines.append("Relevant entities:")
            for item in entities[:8]:
                lines.append(f"- {item}")
        if open_questions:
            lines.append("Open questions:")
            for item in open_questions[:5]:
                lines.append(f"- {item[:180]}")

        summary = "\n".join(lines).strip()
        if len(summary) > 2400:
            summary = summary[:2397].rsplit("\n", 1)[0] + "..."
        return summary


class TopicDetailStateService:
    def __init__(self, message_repository: MessageRepository, topic_repository: TopicRepository) -> None:
        self.message_repository = message_repository
        self.topic_repository = topic_repository

    def rebuild(self, topic_id: int, topic_name: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        messages = self.message_repository.get_topic_messages(topic_id)
        state, change_log = _derive_topic_state(topic_name, messages)
        summary = _summary_from_topic_state(state)
        self.topic_repository.update_state(topic_id, state, change_log, summary)
        return state, change_log, summary


class TokenBudgetManager:
    def __init__(self, config: MemoryConfig) -> None:
        self.config = config

    def apply(self, blocks: list[str]) -> tuple[list[str], int]:
        selected: list[str] = []
        spent = 0
        for block in blocks:
            token_estimate = _estimate_tokens(block)
            if selected and spent + token_estimate > self.config.maximum_memory_tokens:
                break
            if not selected and token_estimate > self.config.maximum_memory_tokens:
                continue
            selected.append(block)
            spent = spent + token_estimate
        return selected, spent


class MemoryContextBuilder:
    def __init__(self, config: MemoryConfig, token_budget_manager: TokenBudgetManager) -> None:
        self.config = config
        self.token_budget_manager = token_budget_manager

    def build(
        self,
        current_topic: TopicRecord,
        related_topics: list[TopicCandidate],
        current_topic_excerpts: list[str],
        supporting_excerpts: dict[int, list[str]],
    ) -> tuple[str, int]:
        blocks: list[str] = []

        current_lines: list[str] = []
        current_lines.append("CURRENT TOPIC")
        current_lines.append(f"Topic: {current_topic.name}")
        current_lines.append(f"Last active: {current_topic.last_active_at}")
        if current_topic.summary.strip():
            current_lines.append("")
            current_lines.append("Summary:")
            current_lines.append(current_topic.summary.strip())
        if current_topic_excerpts:
            current_lines.append("")
            current_lines.append("Supporting excerpts:")
            for excerpt in current_topic_excerpts:
                current_lines.append(f"- {excerpt}")
        blocks.append("\n".join(current_lines).strip())

        related_blocks: list[str] = []
        for candidate in related_topics:
            topic = candidate.topic
            lines: list[str] = []
            lines.append("RELATED TOPIC")
            lines.append(f"Topic: {topic.name}")
            if self.config.diagnostics:
                lines.append(f"Relevance: {candidate.final_score:.3f}")
            lines.append(f"Last active: {topic.last_active_at}")
            lines.append("")
            lines.append("Summary:")
            lines.append(topic.summary.strip() or "- No summary yet.")

            excerpts = supporting_excerpts.get(topic.id, [])
            if excerpts:
                lines.append("")
                lines.append("Supporting excerpts:")
                for excerpt in excerpts:
                    lines.append(f"- {excerpt}")
            related_blocks.append("\n".join(lines).strip())

        merged_blocks = blocks + related_blocks
        selected_blocks, spent_tokens = self.token_budget_manager.apply(merged_blocks)

        output_lines: list[str] = ["PERSISTENT MEMORY CONTEXT", ""]
        output_lines.extend(selected_blocks)
        output_lines.append("")
        output_lines.append("END PERSISTENT MEMORY CONTEXT")
        return "\n".join(output_lines).strip(), spent_tokens


class TopicRetriever:
    def __init__(
        self,
        config: MemoryConfig,
        classifier: TopicClassifier,
        message_repository: MessageRepository,
    ) -> None:
        self.config = config
        self.classifier = classifier
        self.message_repository = message_repository

    def select_related_topics(
        self,
        user_message: str,
        ranked_candidates: list[TopicCandidate],
        current_topic_id: int,
    ) -> tuple[list[TopicCandidate], dict[int, list[str]], list[dict[str, Any]]]:
        selected: list[TopicCandidate] = []
        diagnostics: list[dict[str, Any]] = []

        for candidate in ranked_candidates:
            included = candidate.final_score >= self.config.minimum_retrieval_score
            if candidate.topic.id == current_topic_id:
                included = False
            if included and len(selected) < self.config.max_retrieved_topics:
                selected.append(candidate)
            diagnostics.append(
                {
                    "topic_id": candidate.topic.id,
                    "topic": candidate.topic.name,
                    "final_score": candidate.final_score,
                    "included": included and candidate.topic.id != current_topic_id,
                }
            )

        if not selected:
            fallback_min_score = max(0.18, self.config.minimum_retrieval_score - 0.10)
            for index, candidate in enumerate(ranked_candidates):
                if candidate.topic.id == current_topic_id:
                    continue
                if candidate.final_score < fallback_min_score:
                    continue
                if candidate.semantic_similarity <= 0.0:
                    continue
                selected.append(candidate)
                if index < len(diagnostics):
                    diagnostics[index]["included"] = True
                    diagnostics[index]["selection_reason"] = "fallback_best_match"
                break

        excerpts = self._supporting_excerpts(user_message, selected)
        return selected, excerpts, diagnostics

    def current_topic_excerpts(self, user_message: str, topic_id: int) -> list[str]:
        topics = [
            TopicCandidate(
                topic=TopicRecord(
                    id=topic_id,
                    name="",
                    summary="",
                    created_at="",
                    updated_at="",
                    last_active_at="",
                    status="active",
                ),
                semantic_similarity=0.0,
                current_topic_bonus=0.0,
                recency_bonus=0.0,
                final_score=0.0,
            )
        ]
        excerpts = self._supporting_excerpts(user_message, topics)
        return excerpts.get(topic_id, [])

    def _supporting_excerpts(self, user_message: str, topics: list[TopicCandidate]) -> dict[int, list[str]]:
        if self.config.maximum_supporting_excerpts <= 0 or self.config.maximum_supporting_excerpts_per_topic <= 0:
            return {}

        query_tokens = _token_set(user_message)
        anaphoric_query = _is_anaphoric_query(user_message)
        collected = 0
        output: dict[int, list[str]] = {}

        for candidate in topics:
            if collected >= self.config.maximum_supporting_excerpts:
                break
            messages = self.message_repository.get_recent_topic_messages(candidate.topic.id, limit=30)
            scored: list[tuple[float, str]] = []
            for message in messages:
                role = str(message.get("role", "")).strip().lower()
                content = str(message.get("content", "")).strip()
                if not content:
                    continue
                overlap = _token_overlap_score(query_tokens, _token_set(content))
                priority = 0.0
                if role == "assistant":
                    if _looks_like_recommendation_or_price(content):
                        priority = priority + 0.45
                    else:
                        priority = priority + 0.20
                if _contains_price_signal(content):
                    priority = priority + 0.20
                if overlap <= 0.0 and not (anaphoric_query and priority > 0.0):
                    continue
                text = _clean_excerpt(content, self.config.maximum_excerpt_chars)
                scored.append((overlap + priority, text))
            scored.sort(key=lambda item: item[0], reverse=True)
            per_topic: list[str] = []
            for _, excerpt in scored[: self.config.maximum_supporting_excerpts_per_topic]:
                if collected >= self.config.maximum_supporting_excerpts:
                    break
                per_topic.append(excerpt)
                collected = collected + 1
            if per_topic:
                output[candidate.topic.id] = per_topic

        return output


class TopicMemoryService:
    def __init__(self, config: MemoryConfig) -> None:
        self.config = config
        self.database = SQLiteDatabase(config.database_path)
        self._ensure_schema()
        self.conversations = ConversationRepository(self.database)
        self.topics = TopicRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.classifier = TopicClassifier(config)
        self.summary_service = TopicSummaryService(config, self.messages, self.topics)
        self.detail_state_service = TopicDetailStateService(self.messages, self.topics)
        self.token_budget = TokenBudgetManager(config)
        self.context_builder = MemoryContextBuilder(config, self.token_budget)
        self.retriever = TopicRetriever(config, self.classifier, self.messages)

    def ensure_conversation(self, conversation_id: str, title: str | None = None) -> None:
        self.conversations.upsert_conversation(conversation_id, title=title)

    def list_conversations(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.conversations.list_recent(limit=limit)

    def list_topics(self, limit: int = 20) -> list[TopicRecord]:
        return self.topics.list_recent(limit=limit)

    def get_topic(self, name_or_id: str) -> TopicRecord | None:
        return self.topics.find_topic(name_or_id)

    def prepare_user_turn(self, conversation_id: str, conversation_title: str | None, user_message: str) -> PreparedMemoryContext:
        self.ensure_conversation(conversation_id, title=conversation_title)
        current_topic_id = self.conversations.get_current_topic_id(conversation_id)
        all_topics = self.topics.list_candidates()
        selected_candidate, ranked = self.classifier.classify(user_message, all_topics, current_topic_id=current_topic_id)

        if selected_candidate is None:
            topic_name = _generate_topic_name(user_message)
            selected_topic = self.topics.create_topic(topic_name)
        else:
            selected_topic = selected_candidate.topic

        self.topics.touch_topic(selected_topic.id)
        self.conversations.set_current_topic(conversation_id, selected_topic.id)
        user_message_id = self.messages.add_message(conversation_id, selected_topic.id, "user", user_message)

        refreshed_current = self.topics.get_topic(selected_topic.id)
        current_topic_messages = self.messages.get_recent_topic_messages(refreshed_current.id, limit=40)
        related_topics, supporting_excerpts, selection_diagnostics = self.retriever.select_related_topics(
            user_message,
            ranked,
            current_topic_id=selected_topic.id,
        )
        current_topic_excerpts = self.retriever.current_topic_excerpts(user_message, refreshed_current.id)
        context_block, spent_tokens = self.context_builder.build(
            current_topic=refreshed_current,
            related_topics=related_topics,
            current_topic_excerpts=current_topic_excerpts,
            supporting_excerpts=supporting_excerpts,
        )
        current_state = _normalize_topic_state(self.topics.get_state(refreshed_current.id), topic_id=refreshed_current.id, topic_name=refreshed_current.name)
        current_version = int(current_state.get("version", 1))

        diagnostics = {
            "conversation_id": conversation_id,
            "current_topic_id": refreshed_current.id,
            "current_topic_version": current_version,
            "candidate_topics": [
                {
                    "topic_id": item.topic.id,
                    "topic": item.topic.name,
                    "semantic_similarity": item.semantic_similarity,
                    "current_topic_bonus": item.current_topic_bonus,
                    "recency_bonus": item.recency_bonus,
                    "final_score": item.final_score,
                }
                for item in ranked
            ],
            "selected_topics": selection_diagnostics,
            "current_topic_excerpts": current_topic_excerpts,
            "public_recall": self.build_public_recall(refreshed_current.id, [candidate.topic.id for candidate in related_topics]),
            "token_budget": {
                "maximum_memory_tokens": self.config.maximum_memory_tokens,
                "spent": spent_tokens,
            },
        }

        return PreparedMemoryContext(
            conversation_id=conversation_id,
            topic_id=refreshed_current.id,
            topic_name=refreshed_current.name,
            user_message_id=user_message_id,
            user_message=user_message,
            expected_version=current_version,
            related_topic_ids=[candidate.topic.id for candidate in related_topics],
            context_block=context_block,
            diagnostics=diagnostics,
        )

    def finalize_assistant_turn(
        self,
        prepared: PreparedMemoryContext,
        assistant_message: str,
        *,
        topic_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.messages.add_message(prepared.conversation_id, prepared.topic_id, "assistant", assistant_message)
        self.topics.touch_topic(prepared.topic_id)

        current_state = _normalize_topic_state(self.topics.get_state(prepared.topic_id), topic_id=prepared.topic_id, topic_name=prepared.topic_name)
        operations: list[dict[str, Any]] = []
        scope: dict[str, Any] | None = None
        expected_version = prepared.expected_version
        if isinstance(topic_patch, dict):
            patch_operations = topic_patch.get("operations")
            if isinstance(patch_operations, list):
                operations = [item for item in patch_operations if isinstance(item, dict)]
            patch_scope = topic_patch.get("scope")
            if isinstance(patch_scope, dict):
                scope = patch_scope
            patch_version = topic_patch.get("expected_version")
            if isinstance(patch_version, int):
                expected_version = patch_version

        if not operations:
            operations, scope = _build_patch_operations_from_turn(
                current_state=current_state,
                topic_name=prepared.topic_name,
                user_message=prepared.user_message,
                assistant_message=assistant_message,
            )

        apply_result = self.topics.apply_operations(
            prepared.topic_id,
            operations,
            expected_version=expected_version,
            scope=scope,
            source={
                "conversation_id": prepared.conversation_id,
                "user_message_id": prepared.user_message_id,
            },
        )
        recall = self.build_public_recall(prepared.topic_id, prepared.related_topic_ids)
        if isinstance(recall, dict):
            recall["patch_result"] = apply_result
        return recall

    def get_current_topic_for_conversation(self, conversation_id: str) -> TopicRecord | None:
        topic_id = self.conversations.get_current_topic_id(conversation_id)
        if topic_id is None:
            return None
        try:
            return self.topics.get_topic(topic_id)
        except ValueError:
            return None

    def undo_topic(self, name_or_id: str) -> tuple[bool, str]:
        topic = self.get_topic(name_or_id)
        if topic is None:
            return False, "Topic not found."
        return self.topics.undo_last_change(topic.id)

    def undo_current_topic(self, conversation_id: str) -> tuple[bool, str]:
        topic = self.get_current_topic_for_conversation(conversation_id)
        if topic is None:
            return False, "No active topic is set for this conversation."
        return self.topics.undo_last_change(topic.id)

    def build_public_recall(self, current_topic_id: int, related_topic_ids: list[int]) -> dict[str, Any]:
        current_topic = self.topics.get_topic(current_topic_id)
        current_state = self.topics.get_state(current_topic_id)
        current_changes = self.topics.get_change_log(current_topic_id)
        related_topics: list[dict[str, Any]] = []
        for related_topic_id in related_topic_ids:
            topic = self.topics.get_topic(related_topic_id)
            related_topics.append(
                _build_public_topic_view(
                    topic,
                    self.topics.get_state(related_topic_id),
                    self.topics.get_change_log(related_topic_id),
                )
            )
        return {
            "current_topic": _build_public_topic_view(current_topic, current_state, current_changes),
            "related_topics": related_topics,
        }

    def get_recent_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        return self.messages.get_recent_conversation_messages(conversation_id, self.config.max_recent_messages)

    def _ensure_schema(self) -> None:
        with self.database.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_archived INTEGER NOT NULL DEFAULT 0,
                    current_topic_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    change_log_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    embedding BLOB,
                    status TEXT NOT NULL DEFAULT 'active'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    topic_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                    FOREIGN KEY(topic_id) REFERENCES topics(id)
                );

                CREATE TABLE IF NOT EXISTS topic_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_topic_id INTEGER NOT NULL,
                    target_topic_id INTEGER NOT NULL,
                    relationship TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_topic_id) REFERENCES topics(id),
                    FOREIGN KEY(target_topic_id) REFERENCES topics(id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conversation_seq ON messages(conversation_id, sequence_number);
                CREATE INDEX IF NOT EXISTS idx_messages_topic_created ON messages(topic_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_topics_last_active ON topics(last_active_at DESC);
                """
            )
            topic_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(topics)").fetchall()}
            if "state_json" not in topic_columns:
                conn.execute("ALTER TABLE topics ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}' ")
            if "change_log_json" not in topic_columns:
                conn.execute("ALTER TABLE topics ADD COLUMN change_log_json TEXT NOT NULL DEFAULT '[]' ")
            conn.commit()


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


def _derive_topic_state(topic_name: str, messages: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    user_messages = [str(item.get("content", "")).strip() for item in messages if str(item.get("role", "")) == "user"]
    all_messages = [str(item.get("content", "")).strip() for item in messages if str(item.get("content", "")).strip()]
    goal = user_messages[0][:220] if user_messages else topic_name
    requirements = _extract_requirements(all_messages)
    if not requirements:
        requirements = _extract_spec_facts(all_messages)

    items_by_id: dict[str, dict[str, Any]] = {}
    change_history: list[dict[str, Any]] = []

    for message in messages:
        role = str(message.get("role", "")).strip().lower()
        content = str(message.get("content", "")).strip()
        created_at = str(message.get("created_at", ""))
        if not content:
            continue

        if role == "assistant":
            for sentence in _split_sentences(content):
                models = _extract_model_mentions([sentence])
                price_value = _extract_price_value(sentence)
                for model in models:
                    item_id = _slugify_name(model)
                    item = items_by_id.get(item_id)
                    if item is None:
                        item = {
                            "id": item_id,
                            "name": model,
                            "status": "active",
                            "facts": {"details": [], "price": None},
                        }
                        items_by_id[item_id] = item
                        change_history.append({"type": "add_item", "item_id": item_id, "message": f"Added {model}", "at": created_at})
                    detail = _clean_excerpt(sentence, 180)
                    details_list = item["facts"].setdefault("details", [])
                    if detail and detail not in details_list:
                        details_list.append(detail)
                    if price_value and item["facts"].get("price") != price_value:
                        item["facts"]["price"] = price_value
                        change_history.append({"type": "update_price", "item_id": item_id, "message": f"Updated price for {model} to {price_value}", "at": created_at})

        if role == "user":
            rejection_target = _extract_action_target(content, action="reject")
            if rejection_target:
                matched = _match_item_id(rejection_target, list(items_by_id.values()))
                if matched is not None and items_by_id[matched].get("status") != "rejected":
                    items_by_id[matched]["status"] = "rejected"
                    items_by_id[matched]["rejection_reason"] = content[:180]
                    change_history.append({"type": "reject_item", "item_id": matched, "message": f"Rejected {items_by_id[matched]['name']}", "at": created_at})
            restore_target = _extract_action_target(content, action="restore")
            if restore_target:
                matched = _match_item_id(restore_target, list(items_by_id.values()))
                if matched is not None and items_by_id[matched].get("status") == "rejected":
                    items_by_id[matched]["status"] = "active"
                    items_by_id[matched].pop("rejection_reason", None)
                    change_history.append({"type": "restore_item", "item_id": matched, "message": f"Restored {items_by_id[matched]['name']}", "at": created_at})

    items = [item for item in items_by_id.values() if item.get("status") != "rejected"]
    rejected_items = [item for item in items_by_id.values() if item.get("status") == "rejected"]
    state = {
        "schema": "topic_detail_state_v1",
        "title": topic_name,
        "goal": goal,
        "requirements": requirements,
        "items": items,
        "rejected_items": rejected_items,
        "open_questions": _extract_open_questions(user_messages),
    }
    normalized_state = _normalize_topic_state(state, topic_id=0, topic_name=topic_name)
    return normalized_state, change_history


def _summary_from_topic_state(state: dict[str, Any]) -> str:
    normalized = _normalize_topic_state(state, topic_id=0, topic_name=str(state.get("title", "General") or "General"))
    title = str(normalized.get("title", "General")).strip() or "General"
    lines = [f"Topic: {title}"]
    goal = str(normalized.get("goal", "")).strip()
    if goal:
        lines.append("Goal:")
        lines.append(f"- {goal[:220]}")
    requirements = normalized.get("requirements", []) if isinstance(normalized.get("requirements"), list) else []
    if requirements:
        lines.append("Requirements:")
        for item in requirements[:6]:
            if isinstance(item, dict):
                lines.append(f"- {str(item.get('label', ''))[:180]}")
            else:
                lines.append(f"- {str(item)[:180]}")
    items = normalized.get("items", []) if isinstance(normalized.get("items"), list) else []
    if items:
        lines.append("Active items:")
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {str(item.get('name', ''))[:180]}")
    rejected_items = normalized.get("rejected_items", []) if isinstance(normalized.get("rejected_items"), list) else []
    if rejected_items:
        lines.append("Rejected items:")
        for item in rejected_items[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {str(item.get('name', ''))[:180]}")
    open_questions = normalized.get("open_questions", []) if isinstance(normalized.get("open_questions"), list) else []
    if open_questions:
        lines.append("Open questions:")
        for item in open_questions[:5]:
            if isinstance(item, dict):
                text = str(item.get("text", "")).strip()
                if text:
                    lines.append(f"- {text[:180]}")
            else:
                lines.append(f"- {str(item)[:180]}")
    return "\n".join(lines).strip()


def _slugify_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return normalized or "item"


def _split_sentences(text: str) -> list[str]:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+|\n+", text or "") if item.strip()]
    return sentences if sentences else [str(text or "").strip()]


def _extract_price_value(text: str) -> str | None:
    patterns = [
        r"\$\d[\d,]*(?:\s*(?:-|to|and)\s*\$?\d[\d,]*)",
        r"\$\d[\d,]*",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match is not None:
            return re.sub(r"\s+", " ", match.group(0)).strip()
    return None


def _extract_action_target(text: str, *, action: str) -> str:
    lowered = (text or "").strip()
    if not lowered:
        return ""
    patterns = {
        "reject": [r"don't want\s+(.+)$", r"do not want\s+(.+)$", r"remove\s+(.+)$", r"reject\s+(.+)$", r"eliminate\s+(.+)$"],
        "restore": [r"put\s+(.+)\s+back", r"restore\s+(.+)$", r"bring\s+(.+)\s+back"],
    }
    for pattern in patterns.get(action, []):
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match is not None:
            return match.group(1).strip(" .?!")
    return ""


def _match_item_id(target: str, items: list[dict[str, Any]]) -> str | None:
    normalized_target = _token_set(target)
    if not normalized_target:
        return None
    best_item_id: str | None = None
    best_score = 0.0
    for item in items:
        item_id = str(item.get("id", "")).strip()
        item_name = str(item.get("name", "")).strip()
        if not item_id or not item_name:
            continue
        score = _query_coverage_score(normalized_target, _token_set(item_name))
        if score > best_score:
            best_score = score
            best_item_id = item_id
    if best_score <= 0.0:
        return None
    return best_item_id


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


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def _topic_basis(topic: TopicRecord) -> str:
    if topic.summary.strip():
        return f"{topic.name}\n{topic.summary}"
    return topic.name


def _semantic_similarity(text_a: str, text_b: str) -> float:
    tokens_a = _token_set(text_a)
    tokens_b = _token_set(text_b)
    overlap = _token_overlap_score(tokens_a, tokens_b)
    query_coverage = _query_coverage_score(tokens_a, tokens_b)
    sequence = SequenceMatcher(None, (text_a or "").lower(), (text_b or "").lower()).ratio()
    blended = (query_coverage * 0.65) + (overlap * 0.2) + (sequence * 0.15)
    return max(0.0, min(1.0, blended))


def _token_overlap_score(tokens_a: set[str], tokens_b: set[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a.intersection(tokens_b))
    union = len(tokens_a.union(tokens_b))
    if union == 0:
        return 0.0
    return float(intersection) / float(union)


def _query_coverage_score(query_tokens: set[str], target_tokens: set[str]) -> float:
    if not query_tokens or not target_tokens:
        return 0.0
    intersection = len(query_tokens.intersection(target_tokens))
    return float(intersection) / float(len(query_tokens))


def _token_set(text: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "did",
        "do",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "much",
        "me",
        "of",
        "on",
        "or",
        "our",
        "this",
        "these",
        "that",
        "the",
        "their",
        "them",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "you",
        "your",
    }

    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9]{2,}", (text or "").lower()):
        if raw in stopwords:
            continue
        token = _normalize_token(raw)
        if not token or token in stopwords:
            continue
        tokens.add(token)
    return tokens


def _normalize_token(token: str) -> str:
    value = (token or "").strip().lower()
    if not value:
        return ""
    typo_map = {
        "reccommended": "recommended",
        "recomend": "recommend",
        "recomended": "recommended",
    }
    mapped = typo_map.get(value)
    if mapped is not None:
        value = mapped
    if value.endswith("ies") and len(value) > 4:
        return value[:-3] + "y"
    if value.endswith("es") and len(value) > 4:
        return value[:-2]
    if value.endswith("s") and len(value) > 3:
        return value[:-1]
    return value


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def _extract_requirements(items: list[str]) -> list[str]:
    output: list[str] = []
    for text in items:
        for line in re.split(r"[\n\r]+", text):
            candidate = line.strip(" -\t")
            lowered = candidate.lower()
            if not candidate:
                continue
            if any(token in lowered for token in ("need", "require", "must", "should", "want", "looking for")):
                output.append(candidate)
    return _dedupe_keep_order(output)


def _extract_spec_facts(items: list[str]) -> list[str]:
    facts: list[str] = []
    patterns = [
        (r"\b32\s*gb\s*ram\b", "32 GB RAM"),
        (r"\b16\s*gb\s*ram\b", "16 GB RAM"),
        (r"\b64\s*gb\s*ram\b", "64 GB RAM"),
        (r"\b1\s*tb\s*ssd\b", "1 TB SSD"),
        (r"\b2\s*tb\s*ssd\b", "2 TB SSD"),
        (r"\btouch\s*screen|touchscreen\b", "Touchscreen"),
        (r"\bflip\s*screen\b|\b360[- ]degree\b|\bconvertible\b|\b2[- ]in[- ]1\b", "Convertible or flip-screen design"),
    ]
    for text in items:
        lowered = (text or "").lower()
        for pattern, label in patterns:
            if re.search(pattern, lowered):
                facts.append(label)
    return _dedupe_keep_order(facts)


def _extract_decisions(items: list[str]) -> list[str]:
    output: list[str] = []
    for text in items:
        for line in re.split(r"[\n\r]+", text):
            candidate = line.strip(" -\t")
            lowered = candidate.lower()
            if not candidate:
                continue
            if any(token in lowered for token in ("suggest", "recommend", "recommended", "decide", "selected", "reject", "prefer", "compared")):
                output.append(candidate)
    return _dedupe_keep_order(output)


def _extract_recommendation_highlights(items: list[str]) -> list[str]:
    output: list[str] = []
    for text in items:
        for line in re.split(r"[\n\r]+", text):
            candidate = line.strip(" -\t")
            lowered = candidate.lower()
            if not candidate:
                continue
            if _looks_like_recommendation_or_price(candidate) or _contains_price_signal(candidate):
                output.append(candidate)
    return _dedupe_keep_order(output)


def _extract_model_mentions(items: list[str]) -> list[str]:
    brands = "HP|Lenovo|Dell|Microsoft|Asus|Acer|MSI|Razer|Samsung|LG|Framework|Alienware"
    brand_values = {"hp", "lenovo", "dell", "microsoft", "asus", "acer", "msi", "razer", "samsung", "lg", "framework", "alienware"}
    families = "Spectre|ThinkPad|Yoga|XPS|Surface|EliteBook|Latitude|OmniBook|Zenbook|ProBook|Pavilion|ZBook"
    pattern = re.compile(
        rf"\b(?:{brands})\s+(?:[A-Z][a-zA-Z0-9-]*\s+){{0,4}}(?:{families}|[A-Z][a-zA-Z0-9-]+)(?:\s+[A-Za-z0-9][a-zA-Z0-9-]*){{0,4}}",
    )
    matches: list[str] = []
    for text in items:
        for match in pattern.findall(text or ""):
            cleaned = re.sub(r"\s+", " ", match).strip(" ,.;:-")
            cleaned = re.sub(r"\b(starts?\s+at|starting\s+at|around|about|from)\b.*$", "", cleaned, flags=re.IGNORECASE).strip(" ,.;:-")
            cleaned = re.sub(r"\b(is|are|can|could|may|might|with|for|to)$", "", cleaned, flags=re.IGNORECASE).strip(" ,.;:-")
            if len(cleaned) >= 4:
                if " and " in cleaned.lower():
                    parts = [part.strip(" ,.;:-") for part in re.split(r"\s+and\s+", cleaned, flags=re.IGNORECASE) if part.strip()]
                    for part in parts:
                        if len(part) >= 4 and part.lower() not in brand_values:
                            matches.append(part)
                    continue
                if cleaned.lower() in brand_values:
                    continue
                matches.append(cleaned)
    return _dedupe_keep_order(matches)


def _extract_open_questions(items: list[str]) -> list[str]:
    output: list[str] = []
    for text in items:
        for sentence in re.split(r"(?<=[?])\s+", text):
            candidate = sentence.strip()
            if candidate.endswith("?"):
                output.append(candidate)
    return _dedupe_keep_order(output)


def _extract_entities(items: list[str]) -> list[str]:
    entities: list[str] = []
    pattern = re.compile(r"\b[A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){0,3}\b")
    stopwords = {"For", "The", "This", "That", "And", "But", "RAM", "SSD"}
    for text in items:
        for match in pattern.findall(text or ""):
            candidate = match.strip()
            if len(candidate) < 3:
                continue
            if candidate in stopwords:
                continue
            entities.append(candidate)
    return _dedupe_keep_order(entities)


def _clean_excerpt(text: str, maximum_chars: int) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if len(compact) <= maximum_chars:
        return compact
    return compact[: maximum_chars - 3].rstrip() + "..."


def _contains_price_signal(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    if "$" in lowered:
        return True
    if re.search(r"\b\d{3,5}\b", lowered) and any(token in lowered for token in ("price", "cost", "usd", "dollar", "starting at", "around", "approximately")):
        return True
    return False


def _looks_like_recommendation_or_price(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    if any(token in lowered for token in ("recommend", "suggest", "option", "model", "configuration", "spec", "ram", "ssd", "price", "cost")):
        return True
    if _contains_price_signal(text):
        return True
    return False


def _is_anaphoric_query(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    markers = (
        "those ",
        "that ",
        "these ",
        "earlier",
        "before",
        "previous",
        "recommended",
        "mentioned",
        "we discussed",
        "you suggested",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_topic_mutation(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    markers = (
        "remove ",
        "reject ",
        "eliminate ",
        "don't want ",
        "do not want ",
        "restore ",
        "put ",
        "bring ",
        "show me the pricing",
        "what models",
        "which has",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_topic_enrichment(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    markers = (
        "price",
        "pricing",
        "cost",
        "battery",
        "display",
        "compare",
        "configuration",
        "ram",
        "ssd",
        "spec",
        "details",
        "for those",
        "for the remaining",
    )
    return any(marker in lowered for marker in markers)


def _generate_topic_name(user_message: str) -> str:
    cleaned = re.sub(r"\s+", " ", (user_message or "").strip())
    if not cleaned:
        return "General"
    lowered = cleaned.lower()
    if "azure" in lowered and "function" in lowered:
        return "Azure Function deployment"
    if "laptop" in lowered or any(token in lowered for token in ("touchscreen", "flip screen", "convertible", "spectre", "thinkpad", "xps", "elitebook", "latitude")):
        if any(token in lowered for token in ("price", "pricing", "cost")):
            return "Laptop pricing"
        return "Convertible laptop research"
    lowered = re.sub(r"^(please|can you|could you|help me|now|and)\s+", "", lowered)
    stopwords = {
        "the",
        "a",
        "an",
        "to",
        "for",
        "with",
        "about",
        "how",
        "what",
        "why",
        "when",
        "where",
        "were",
        "was",
        "is",
        "are",
        "those",
        "these",
        "this",
        "that",
        "you",
        "your",
        "our",
        "much",
    }
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+", lowered):
        normalized = _normalize_token(token)
        if not normalized or normalized in stopwords:
            continue
        tokens.append(normalized)
    if not tokens:
        return "General"
    candidate = " ".join(tokens[:5]).strip()
    return candidate[:1].upper() + candidate[1:]


def _build_public_topic_view(topic: TopicRecord, state: dict[str, Any], change_log: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = _normalize_topic_state(state, topic_id=topic.id, topic_name=topic.name)
    items = normalized.get("items", []) if isinstance(normalized.get("items"), list) else []
    rejected_items = normalized.get("rejected_items", []) if isinstance(normalized.get("rejected_items"), list) else []
    archived_items = normalized.get("archived_items", []) if isinstance(normalized.get("archived_items"), list) else []
    removed_items = normalized.get("removed_items", []) if isinstance(normalized.get("removed_items"), list) else []
    return {
        "topic_id": topic.id,
        "title": str(normalized.get("title", topic.name)).strip() or topic.name,
        "topic_type": str(normalized.get("topic_type", "generic")).strip() or "generic",
        "summary": topic.summary.strip(),
        "goal": str(normalized.get("goal", "")).strip(),
        "version": int(normalized.get("version", 1)),
        "updated_at": str(normalized.get("updated_at", topic.updated_at)),
        "requirements": normalized.get("requirements", []) if isinstance(normalized.get("requirements"), list) else [],
        "items": [item for item in items if isinstance(item, dict)],
        "rejected_items": [item for item in rejected_items if isinstance(item, dict)],
        "archived_items": [item for item in archived_items if isinstance(item, dict)],
        "removed_items": [item for item in removed_items if isinstance(item, dict)],
        "decisions": normalized.get("decisions", []) if isinstance(normalized.get("decisions"), list) else [],
        "open_questions": normalized.get("open_questions", []) if isinstance(normalized.get("open_questions"), list) else [],
        "notes": normalized.get("notes", []) if isinstance(normalized.get("notes"), list) else [],
        "change_history": [item for item in change_log if isinstance(item, dict)],
    }


def _normalize_topic_state(state: dict[str, Any], *, topic_id: int, topic_name: str) -> dict[str, Any]:
    value = state if isinstance(state, dict) else {}
    try:
        version_value = int(value.get("version", 1)) if str(value.get("version", "")).strip() else 1
    except (TypeError, ValueError):
        version_value = 1
    requirements_input = value.get("requirements", []) if isinstance(value.get("requirements"), list) else []
    requirements: list[dict[str, Any]] = []
    seen_requirement_ids: set[str] = set()
    for item in requirements_input:
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            req_id = str(item.get("id", "")).strip() or _slugify_name(label)
            status = str(item.get("status", "required")).strip() or "required"
        else:
            label = str(item).strip()
            req_id = _slugify_name(label)
            status = "required"
        if not label or not req_id or req_id in seen_requirement_ids:
            continue
        seen_requirement_ids.add(req_id)
        requirements.append({"id": req_id, "label": label, "status": status})

    result = {
        "schema": "topic_state_v2",
        "topic_id": str(value.get("topic_id") or topic_id),
        "title": str(value.get("title") or topic_name or "General").strip() or "General",
        "topic_type": str(value.get("topic_type") or "generic").strip() or "generic",
        "goal": str(value.get("goal") or "").strip(),
        "requirements": requirements,
        "items": _normalize_item_collection(value.get("items")),
        "rejected_items": _normalize_item_collection(value.get("rejected_items")),
        "archived_items": _normalize_item_collection(value.get("archived_items")),
        "removed_items": _normalize_item_collection(value.get("removed_items")),
        "decisions": _normalize_text_entries(value.get("decisions")),
        "open_questions": _normalize_open_questions(value.get("open_questions")),
        "notes": _normalize_text_entries(value.get("notes")),
        "version": max(1, version_value),
        "updated_at": str(value.get("updated_at") or _utc_now()),
    }
    return result


def _normalize_item_collection(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip() or _slugify_name(str(item.get("name", "")).strip())
        name = str(item.get("name", "")).strip()
        if not item_id or not name or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        facts = item.get("facts", {}) if isinstance(item.get("facts"), dict) else {}
        details = facts.get("details", []) if isinstance(facts.get("details"), list) else []
        attributes = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
        if "price" in facts and "price" not in attributes and facts.get("price"):
            attributes["price"] = str(facts.get("price"))
        output.append(
            {
                "id": item_id,
                "name": name,
                "status": str(item.get("status", "active") or "active"),
                "attributes": {str(key): value for key, value in attributes.items() if _is_valid_attribute_name(str(key))},
                "facts": {
                    "details": [str(detail).strip() for detail in details if str(detail).strip()],
                    "price": str(facts.get("price", "")).strip() if facts.get("price") is not None else None,
                },
                "pros": [str(entry).strip() for entry in item.get("pros", []) if str(entry).strip()] if isinstance(item.get("pros"), list) else [],
                "cons": [str(entry).strip() for entry in item.get("cons", []) if str(entry).strip()] if isinstance(item.get("cons"), list) else [],
                "aliases": [str(entry).strip() for entry in item.get("aliases", []) if str(entry).strip()] if isinstance(item.get("aliases"), list) else [],
                "manufacturer": str(item.get("manufacturer", "")).strip(),
                "model_number": str(item.get("model_number", "")).strip(),
                "source_refs": [entry for entry in item.get("source_refs", []) if isinstance(entry, dict)] if isinstance(item.get("source_refs"), list) else [],
                "rejection_reason": str(item.get("rejection_reason", "")).strip(),
            }
        )
    return output


def _normalize_open_questions(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            question_id = str(item.get("id", "")).strip() or _slugify_name(text)
            status = str(item.get("status", "open")).strip() or "open"
        else:
            text = str(item).strip()
            question_id = _slugify_name(text)
            status = "open"
        if not text or not question_id or question_id in seen_ids:
            continue
        seen_ids.add(question_id)
        output.append({"id": question_id, "text": text, "status": status})
    return output


def _normalize_text_entries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _build_patch_operations_from_turn(
    *,
    current_state: dict[str, Any],
    topic_name: str,
    user_message: str,
    assistant_message: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    normalized_state = _normalize_topic_state(current_state, topic_id=0, topic_name=topic_name)
    scope = _derive_patch_scope(normalized_state, user_message)
    allow_new_items = bool(scope.get("allow_new_items", True))

    if not str(normalized_state.get("title", "")).strip():
        operations.append({"op": "set_topic_title", "value": topic_name})

    if not str(normalized_state.get("goal", "")).strip() and user_message.strip():
        operations.append({"op": "set_goal", "value": user_message.strip()[:220]})

    for requirement in _extract_spec_facts([user_message]):
        operations.append({"op": "add_requirement", "id": _slugify_name(requirement), "label": requirement, "status": "required"})

    for requirement in _extract_requirements([user_message]):
        operations.append({"op": "add_requirement", "id": _slugify_name(requirement), "label": requirement, "status": "required"})

    for model in _extract_model_mentions([assistant_message]):
        item_id = _slugify_name(model)
        existing_id = _resolve_item_reference_id(normalized_state, item_id) or _resolve_item_reference_id(normalized_state, model)
        if existing_id is not None:
            continue
        if allow_new_items:
            operations.append(
                {
                    "op": "add_item",
                    "item": {
                        "id": item_id,
                        "name": model,
                        "status": "active",
                        "attributes": {},
                        "facts": {"details": []},
                    },
                }
            )

    for sentence in _split_sentences(assistant_message):
        sentence_models = _extract_model_mentions([sentence])
        if not sentence_models:
            continue
        for model in sentence_models:
            item_id = _slugify_name(model)
            resolved_item_id = _resolve_item_reference_id(normalized_state, item_id) or _resolve_item_reference_id(normalized_state, model)
            if resolved_item_id is not None:
                item_id = resolved_item_id
            elif not allow_new_items:
                continue
            price = _extract_price_value(sentence)
            if price:
                operations.append(
                    {
                        "op": "set_item_attribute",
                        "item_id": item_id,
                        "field": "price",
                        "value": price,
                    }
                )
            detail = _clean_excerpt(sentence, 180)
            if detail:
                operations.append({"op": "update_item", "item_id": item_id, "add_detail": detail})

    reject_target = _extract_action_target(user_message, action="reject")
    if reject_target:
        matched_ids = _resolve_item_reference_ids(normalized_state, reject_target)
        if matched_ids:
            for item_id in matched_ids:
                operations.append({"op": "reject_item", "item_id": item_id, "reason": user_message[:180]})
        else:
            operations.append({"op": "reject_item", "item_id": reject_target, "reason": user_message[:180]})

    restore_target = _extract_action_target(user_message, action="restore")
    if restore_target:
        operations.append({"op": "restore_item", "item_id": restore_target})

    lower_user = user_message.strip().lower()
    if "only keep" in lower_user or "best three" in lower_user:
        selected = [_slugify_name(item) for item in _extract_model_mentions([assistant_message])]
        if selected:
            active_items = normalized_state.get("items", []) if isinstance(normalized_state.get("items"), list) else []
            for item in active_items:
                if not isinstance(item, dict):
                    continue
                candidate_id = str(item.get("id", "")).strip()
                if not candidate_id or candidate_id in selected:
                    continue
                operations.append({"op": "reject_item", "item_id": candidate_id, "reason": "Moved out of active selection"})

    return operations[:100], scope


def _derive_patch_scope(state: dict[str, Any], user_message: str) -> dict[str, Any]:
    active_items = state.get("items", []) if isinstance(state.get("items"), list) else []
    active_item_ids = [str(item.get("id", "")).strip() for item in active_items if isinstance(item, dict) and str(item.get("id", "")).strip()]
    has_active_items = bool(active_item_ids)

    if not has_active_items:
        return {
            "mode": "expand_candidates",
            "item_ids": [],
            "allow_new_items": True,
        }

    if _is_scope_expansion_request(user_message):
        return {
            "mode": "expand_candidates",
            "item_ids": active_item_ids,
            "allow_new_items": True,
        }

    return {
        "mode": "all_active_items",
        "item_ids": active_item_ids,
        "allow_new_items": False,
    }


def _is_scope_expansion_request(user_message: str) -> bool:
    lowered = (user_message or "").strip().lower()
    if not lowered:
        return False
    markers = (
        "recommend more",
        "add more",
        "add another",
        "broaden",
        "what else should i consider",
        "what else do you recommend",
        "expand",
        "more options",
        "replace these",
    )
    return any(marker in lowered for marker in markers)


def _apply_topic_operation(state: dict[str, Any], operation: dict[str, Any]) -> tuple[bool, str]:
    op_name = str(operation.get("op", "")).strip()
    if not op_name:
        return False, "missing_op"
    supported = {
        "set_topic_title",
        "set_goal",
        "add_requirement",
        "update_requirement",
        "remove_requirement",
        "add_item",
        "update_item",
        "set_item_attribute",
        "remove_item_attribute",
        "reject_item",
        "restore_item",
        "remove_item",
        "add_decision",
        "replace_decision",
        "remove_decision",
        "add_open_question",
        "resolve_open_question",
        "add_note",
        "remove_note",
    }
    if op_name not in supported:
        return False, "unsupported_operation"

    if op_name == "set_topic_title":
        value = str(operation.get("value", "")).strip()
        if not value:
            return False, "missing_value"
        state["title"] = value[:220]
        return True, "ok"

    if op_name == "set_goal":
        value = str(operation.get("value", "")).strip()
        if not value:
            return False, "missing_value"
        state["goal"] = value[:400]
        return True, "ok"

    if op_name in {"add_requirement", "update_requirement", "remove_requirement"}:
        requirements = state.get("requirements", []) if isinstance(state.get("requirements"), list) else []
        req_id = str(operation.get("id", "")).strip() or _slugify_name(str(operation.get("label", "")).strip())
        if not req_id:
            return False, "missing_requirement_id"
        if op_name == "remove_requirement":
            for index, item in enumerate(requirements):
                if isinstance(item, dict) and str(item.get("id", "")).strip() == req_id:
                    requirements.pop(index)
                    state["requirements"] = requirements
                    return True, "ok"
            return False, "requirement_not_found"
        label = str(operation.get("label", "")).strip()
        status = str(operation.get("status", "required")).strip() or "required"
        for item in requirements:
            if not isinstance(item, dict):
                continue
            if str(item.get("id", "")).strip() != req_id:
                continue
            if op_name == "add_requirement":
                return False, "requirement_exists"
            if label:
                item["label"] = label[:220]
            item["status"] = status[:80]
            state["requirements"] = requirements
            return True, "ok"
        if not label:
            return False, "missing_requirement_label"
        requirements.append({"id": req_id, "label": label[:220], "status": status[:80]})
        state["requirements"] = requirements
        return True, "ok"

    if op_name in {"add_item", "update_item", "set_item_attribute", "remove_item_attribute", "reject_item", "restore_item", "remove_item"}:
        target_id = str(operation.get("item_id", "")).strip()
        if op_name == "add_item":
            item_payload = operation.get("item") if isinstance(operation.get("item"), dict) else {}
            item_name = str(item_payload.get("name", "")).strip()
            item_id = str(item_payload.get("id", "")).strip() or _slugify_name(item_name)
            if not item_name or not item_id:
                return False, "missing_item_identity"
            existing_id = _resolve_item_reference_id(state, item_id) or _resolve_item_reference_id(state, item_name)
            if existing_id is not None:
                return False, "item_exists"
            new_item = _normalize_item_collection([item_payload])[0] if _normalize_item_collection([item_payload]) else {
                "id": item_id,
                "name": item_name,
                "status": "active",
                "attributes": {},
                "facts": {"details": [], "price": None},
                "pros": [],
                "cons": [],
                "aliases": [],
                "manufacturer": "",
                "model_number": "",
                "source_refs": [],
                "rejection_reason": "",
            }
            active_items = state.get("items", []) if isinstance(state.get("items"), list) else []
            active_items.append(new_item)
            state["items"] = active_items
            return True, "ok"

        resolved_id = _resolve_item_reference_id(state, target_id or str(operation.get("item", "")).strip())
        if resolved_id is None:
            return False, "item_not_found"

        if op_name == "remove_item":
            removed = _remove_item_from_all_collections(state, resolved_id)
            if removed is None:
                return False, "item_not_found"
            removed["status"] = "removed"
            removed_items = state.get("removed_items", []) if isinstance(state.get("removed_items"), list) else []
            removed_items.append(removed)
            state["removed_items"] = removed_items
            return True, "ok"

        if op_name == "reject_item":
            removed = _remove_item_from_all_collections(state, resolved_id)
            if removed is None:
                return False, "item_not_found"
            removed["status"] = "rejected"
            removed["rejection_reason"] = str(operation.get("reason", "Rejected by user")).strip()[:220]
            rejected_items = state.get("rejected_items", []) if isinstance(state.get("rejected_items"), list) else []
            rejected_items.append(removed)
            state["rejected_items"] = rejected_items
            return True, "ok"

        if op_name == "restore_item":
            removed = _remove_item_from_collection(state, "rejected_items", resolved_id)
            if removed is None:
                removed = _remove_item_from_collection(state, "archived_items", resolved_id)
            if removed is None:
                removed = _remove_item_from_collection(state, "removed_items", resolved_id)
            if removed is None:
                return False, "item_not_found"
            removed["status"] = "active"
            removed["rejection_reason"] = ""
            active_items = state.get("items", []) if isinstance(state.get("items"), list) else []
            active_items.append(removed)
            state["items"] = active_items
            return True, "ok"

        item = _find_item_in_collection(state, "items", resolved_id)
        if item is None:
            item = _find_item_in_collection(state, "rejected_items", resolved_id)
        if item is None:
            item = _find_item_in_collection(state, "archived_items", resolved_id)
        if item is None:
            item = _find_item_in_collection(state, "removed_items", resolved_id)
        if item is None:
            return False, "item_not_found"

        if op_name == "update_item":
            name = str(operation.get("name", "")).strip()
            if name:
                item["name"] = name[:220]
            detail = str(operation.get("add_detail", "")).strip()
            if detail:
                facts = item.get("facts", {}) if isinstance(item.get("facts"), dict) else {}
                details = facts.get("details", []) if isinstance(facts.get("details"), list) else []
                if detail not in details:
                    details.append(detail)
                facts["details"] = details[:15]
                item["facts"] = facts
            return True, "ok"

        if op_name == "set_item_attribute":
            field = str(operation.get("field", "")).strip().lower()
            if not _is_valid_attribute_name(field):
                return False, "invalid_field"
            attributes = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            attributes[field] = operation.get("value")
            item["attributes"] = attributes
            if field == "price":
                facts = item.get("facts", {}) if isinstance(item.get("facts"), dict) else {}
                facts["price"] = str(operation.get("value", "")).strip()
                item["facts"] = facts
            return True, "ok"

        if op_name == "remove_item_attribute":
            field = str(operation.get("field", "")).strip().lower()
            if not _is_valid_attribute_name(field):
                return False, "invalid_field"
            attributes = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            if field not in attributes:
                return False, "attribute_not_found"
            attributes.pop(field, None)
            item["attributes"] = attributes
            if field == "price":
                facts = item.get("facts", {}) if isinstance(item.get("facts"), dict) else {}
                facts["price"] = None
                item["facts"] = facts
            return True, "ok"

    if op_name in {"add_decision", "replace_decision", "remove_decision"}:
        decisions = state.get("decisions", []) if isinstance(state.get("decisions"), list) else []
        if op_name == "add_decision":
            value = str(operation.get("value", "")).strip()
            if not value:
                return False, "missing_value"
            if value not in decisions:
                decisions.append(value[:260])
            state["decisions"] = decisions
            return True, "ok"
        if op_name == "replace_decision":
            index = operation.get("index")
            value = str(operation.get("value", "")).strip()
            if not isinstance(index, int) or index < 0 or index >= len(decisions) or not value:
                return False, "invalid_decision_update"
            decisions[index] = value[:260]
            state["decisions"] = decisions
            return True, "ok"
        value = str(operation.get("value", "")).strip().lower()
        if not value:
            return False, "missing_value"
        filtered = [item for item in decisions if str(item).strip().lower() != value]
        if len(filtered) == len(decisions):
            return False, "decision_not_found"
        state["decisions"] = filtered
        return True, "ok"

    if op_name in {"add_open_question", "resolve_open_question"}:
        questions = state.get("open_questions", []) if isinstance(state.get("open_questions"), list) else []
        if op_name == "add_open_question":
            text = str(operation.get("text", "")).strip()
            if not text:
                return False, "missing_text"
            question_id = str(operation.get("id", "")).strip() or _slugify_name(text)
            for entry in questions:
                if isinstance(entry, dict) and str(entry.get("id", "")).strip() == question_id:
                    return False, "question_exists"
            questions.append({"id": question_id, "text": text[:260], "status": "open"})
            state["open_questions"] = questions
            return True, "ok"
        question_id = str(operation.get("id", "")).strip() or _slugify_name(str(operation.get("text", "")).strip())
        if not question_id:
            return False, "missing_question_id"
        for entry in questions:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id", "")).strip() != question_id:
                continue
            entry["status"] = "resolved"
            state["open_questions"] = questions
            return True, "ok"
        return False, "question_not_found"

    if op_name in {"add_note", "remove_note"}:
        notes = state.get("notes", []) if isinstance(state.get("notes"), list) else []
        value = str(operation.get("value", "")).strip()
        if not value:
            return False, "missing_value"
        if op_name == "add_note":
            if value not in notes:
                notes.append(value[:260])
            state["notes"] = notes
            return True, "ok"
        filtered = [item for item in notes if str(item).strip().lower() != value.lower()]
        if len(filtered) == len(notes):
            return False, "note_not_found"
        state["notes"] = filtered
        return True, "ok"

    return False, "unsupported_operation"


def _resolve_item_reference_id(state: dict[str, Any], token: str) -> str | None:
    matches = _resolve_item_reference_ids(state, token)
    if matches:
        return matches[0]
    return None


def _resolve_item_reference_ids(state: dict[str, Any], token: str) -> list[str]:
    needle = str(token or "").strip()
    if not needle:
        return []
    normalized_needle = _slugify_name(needle)
    needle_tokens = _token_set(needle)
    is_plural_reference = "laptops" in needle.lower() or "models" in needle.lower() or "options" in needle.lower()
    collections = ("items", "rejected_items", "archived_items", "removed_items")
    scored: list[tuple[float, str]] = []
    for name in collections:
        items = state.get(name, []) if isinstance(state.get(name), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            item_name = str(item.get("name", "")).strip()
            aliases = item.get("aliases", []) if isinstance(item.get("aliases"), list) else []
            if needle == item_id or normalized_needle == item_id:
                return [item_id]
            item_slug = _slugify_name(item_name)
            if item_slug == normalized_needle:
                return [item_id]
            for alias in aliases:
                if _slugify_name(str(alias)) == normalized_needle:
                    return [item_id]
            item_tokens = _token_set(item_name)
            if not needle_tokens or not item_tokens:
                continue
            overlap = _query_coverage_score(needle_tokens, item_tokens)
            if overlap > 0.0:
                scored.append((overlap, item_id))

    if not scored:
        return []

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = scored[0][0]
    if is_plural_reference:
        threshold = max(0.60, min(best_score, 0.80))
        selected_ids: list[str] = []
        seen: set[str] = set()
        for score, item_id in scored:
            if score < threshold:
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            selected_ids.append(item_id)
        return selected_ids

    if best_score < 0.70:
        return []
    return [scored[0][1]]


def _remove_item_from_collection(state: dict[str, Any], collection: str, item_id: str) -> dict[str, Any] | None:
    items = state.get(collection, []) if isinstance(state.get(collection), list) else []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() != item_id:
            continue
        removed = items.pop(index)
        state[collection] = items
        return removed
    return None


def _remove_item_from_all_collections(state: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for collection in ("items", "rejected_items", "archived_items", "removed_items"):
        removed = _remove_item_from_collection(state, collection, item_id)
        if removed is not None:
            return removed
    return None


def _find_item_in_collection(state: dict[str, Any], collection: str, item_id: str) -> dict[str, Any] | None:
    items = state.get(collection, []) if isinstance(state.get(collection), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() == item_id:
            return item
    return None


def _is_valid_attribute_name(field: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", field or ""))


def _deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True))


def _trim_change_log(change_log: list[dict[str, Any]], limit: int = 300) -> list[dict[str, Any]]:
    if len(change_log) <= limit:
        return change_log
    return change_log[-limit:]


def _build_patch_policy(state: dict[str, Any], scope: dict[str, Any] | None) -> dict[str, Any]:
    scoped = scope if isinstance(scope, dict) else {}
    active_items = state.get("items", []) if isinstance(state.get("items"), list) else []
    active_item_ids = [str(item.get("id", "")).strip() for item in active_items if isinstance(item, dict) and str(item.get("id", "")).strip()]
    default_mode = "all_active_items" if active_item_ids else "expand_candidates"
    mode = str(scoped.get("mode", default_mode)).strip() or default_mode
    allow_new_items = bool(scoped.get("allow_new_items", mode in {"expand_candidates", "replace_candidate_set"}))
    provided_item_ids = scoped.get("item_ids") if isinstance(scoped.get("item_ids"), list) else []
    item_ids = [str(item).strip() for item in provided_item_ids if str(item).strip()]
    if not item_ids and mode == "all_active_items":
        item_ids = active_item_ids
    return {
        "mode": mode,
        "item_ids": item_ids,
        "allow_new_items": allow_new_items,
    }


def _validate_operation_against_policy(
    state: dict[str, Any],
    operation: dict[str, Any],
    policy: dict[str, Any],
    planned_restore_targets: set[str],
) -> tuple[bool, str]:
    op_name = str(operation.get("op", "")).strip()
    if not op_name:
        return False, "missing_op"

    mode = str(policy.get("mode", "all_active_items")).strip() or "all_active_items"
    allowed_item_ids = {
        str(item).strip()
        for item in policy.get("item_ids", [])
        if str(item).strip()
    }
    allow_new_items = bool(policy.get("allow_new_items", True))

    rejected_items = state.get("rejected_items", []) if isinstance(state.get("rejected_items"), list) else []
    rejected_ids = {
        str(item.get("id", "")).strip()
        for item in rejected_items
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }

    if op_name == "add_item":
        if not allow_new_items:
            return False, "scope_disallows_add_item"
        item_payload = operation.get("item") if isinstance(operation.get("item"), dict) else {}
        item_name = str(item_payload.get("name", "")).strip()
        item_id = str(item_payload.get("id", "")).strip() or _slugify_name(item_name)
        if not item_id:
            return False, "missing_item_identity"
        matched_rejected = _resolve_item_reference_id(state, item_id) or _resolve_item_reference_id(state, item_name)
        if matched_rejected in rejected_ids and matched_rejected not in planned_restore_targets:
            return False, "cannot_readd_rejected_without_restore"
        return True, "ok"

    if op_name in {"update_item", "set_item_attribute", "remove_item_attribute", "reject_item", "remove_item"}:
        target_token = str(operation.get("item_id", "")).strip()
        if not target_token:
            return False, "missing_item_id"
        resolved = _resolve_item_reference_id(state, target_token)
        if resolved is None:
            return False, "item_not_found"
        if mode in {"existing_items_only", "selected_items", "all_active_items"} and allowed_item_ids and resolved not in allowed_item_ids:
            return False, "item_out_of_scope"
        if resolved in rejected_ids and op_name != "restore_item":
            return False, "rejected_item_requires_restore"
        return True, "ok"

    return True, "ok"


def _collect_restore_targets(state: dict[str, Any], operations: list[dict[str, Any]]) -> set[str]:
    output: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if str(operation.get("op", "")).strip() != "restore_item":
            continue
        token = str(operation.get("item_id", "")).strip()
        if not token:
            continue
        resolved = _resolve_item_reference_id(state, token)
        if resolved is not None:
            output.add(resolved)
    return output


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
