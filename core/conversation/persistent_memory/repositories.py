from __future__ import annotations

import json
from typing import Any

from core.conversation.persistent_memory.models import (
    TopicRecord,
    _deep_copy_json,
    _json_list_of_objects,
    _json_object_or_empty,
    _topic_from_row,
    _trim_change_log,
    _utc_now,
)
from core.conversation.persistent_memory.topic_state import (
    _apply_topic_operation,
    _build_patch_policy,
    _collect_restore_targets,
    _normalize_topic_state,
    _summary_from_topic_state,
    _validate_operation_against_policy,
)
from core.storage.sqlite_database import SQLiteDatabase

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
