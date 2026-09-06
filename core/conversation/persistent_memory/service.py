from __future__ import annotations

from typing import Any

from core.conversation.persistent_memory.models import MemoryConfig, PreparedMemoryContext, TopicRecord
from core.conversation.persistent_memory.repositories import (
    ConversationRepository,
    MessageRepository,
    TopicRepository,
)
from core.conversation.persistent_memory.services import (
    MemoryContextBuilder,
    TokenBudgetManager,
    TopicClassifier,
    TopicDetailStateService,
    TopicRetriever,
    TopicSummaryService,
)
from core.conversation.persistent_memory.text import _generate_topic_name
from core.conversation.persistent_memory.topic_state import (
    _build_patch_operations_from_turn,
    _build_public_topic_view,
    _normalize_topic_state,
)
from core.storage.sqlite_database import SQLiteDatabase

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
