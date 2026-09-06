from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.conversation.persistent_memory.models import MemoryConfig, TopicCandidate, TopicRecord, _topic_basis
from core.conversation.persistent_memory.repositories import MessageRepository, TopicRepository
from core.conversation.persistent_memory.text import (
    _clean_excerpt,
    _contains_price_signal,
    _dedupe_keep_order,
    _estimate_tokens,
    _extract_decisions,
    _extract_entities,
    _extract_model_mentions,
    _extract_open_questions,
    _extract_recommendation_highlights,
    _extract_requirements,
    _extract_spec_facts,
    _is_anaphoric_query,
    _looks_like_recommendation_or_price,
    _looks_like_topic_enrichment,
    _looks_like_topic_mutation,
    _semantic_similarity,
    _token_overlap_score,
    _token_set,
)
from core.conversation.persistent_memory.topic_state import _derive_topic_state, _summary_from_topic_state

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
