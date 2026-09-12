# File: core/mcp_server/tools.py

from __future__ import annotations

import logging
from typing import Any, Sequence

from core.knowledge.appraisal import CONTRACT, FAVOURABLE_KEY, Appraiser
from core.knowledge.models import KnowledgeError, MemoryKind, MemoryRecord
from core.knowledge.retrieval import KnowledgeQuery
from core.permissions.models import Decision, PermissionLevel, PermissionRequest
from core.tools.models import ToolKind

logger = logging.getLogger(__name__)

SOURCE = "mcp"

DEFAULT_RECORD_SOURCE = "mcp:caller"

MAX_LIMIT = 50


class McpToolError(RuntimeError):
    pass


class IrisMcpTools:
    def __init__(
        self,
        knowledge: Any,
        retriever: Any,
        review: Any,
        *,
        permissions: Any = None,
        auditor: Any = None,
        client: str = "unknown",
    ) -> None:
        self.knowledge = knowledge
        self.retriever = retriever
        self.review = review
        self.permissions = permissions
        self.auditor = auditor
        self.client = client
        self.appraiser = Appraiser(knowledge, retriever)

    @property
    def source(self) -> str:
        return f"{SOURCE}:{self.client}"

    def recall(
        self,
        text: str,
        topic: str | None = None,
        kinds: Sequence[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        self._permit("recall", PermissionLevel.READ)
        try:
            wanted = tuple(MemoryKind(value) for value in (kinds or ()))
        except ValueError as error:
            raise McpToolError(str(error)) from error
        result = self.retriever.retrieve(
            KnowledgeQuery(
                text=text,
                topic=topic.strip().lower() if topic else None,
                kinds=wanted,
                limit=_bounded(limit),
            )
        )
        payload = {
            "records": [
                {**_record(item.record), "score": round(float(item.score), 4), "reasons": list(item.reasons)}
                for item in result.records
            ]
        }
        return self._done("recall", {"text": text, "topic": topic, "limit": limit}, payload)

    def observe(
        self,
        topic: str,
        content: str,
        source: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._permit("observe", PermissionLevel.WRITE)
        record = self.review.observe(
            _topic(topic), _content(content), source=source or DEFAULT_RECORD_SOURCE, data=dict(data or {})
        )
        return self._done("observe", {"topic": topic, "source": source}, _record(record))

    def close_observation(
        self,
        observation_id: str,
        content: str,
        favourable: bool | None = None,
    ) -> dict[str, Any]:
        self._permit("close_observation", PermissionLevel.WRITE)
        observation = self.knowledge.records.get(observation_id)
        if observation is None:
            raise McpToolError(f"No such observation: {observation_id}")
        if observation.kind is not MemoryKind.OBSERVATION:
            raise McpToolError(f"Outcomes close observations, and that is a {observation.kind.value}")
        data: dict[str, Any] = {} if favourable is None else {FAVOURABLE_KEY: bool(favourable)}
        stored = self.knowledge.record_outcomes(
            [
                (
                    observation_id,
                    MemoryRecord(
                        kind=MemoryKind.OUTCOME,
                        topic=observation.topic,
                        content=_content(content),
                        source=self.source,
                        data=data,
                    ),
                )
            ]
        )
        return self._done("close_observation", {"observation_id": observation_id, "favourable": favourable}, _record(stored[0]))

    def hypothesize(self, topic: str, content: str, source: str | None = None) -> dict[str, Any]:
        self._permit("hypothesize", PermissionLevel.WRITE)
        record = self.review.hypothesize(
            _topic(topic), _content(content), source=source or DEFAULT_RECORD_SOURCE
        )
        return self._done("hypothesize", {"topic": topic, "source": source}, _record(record))

    def add_evidence(
        self,
        hypothesis_id: str,
        record_id: str,
        supports: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        self._permit("add_evidence", PermissionLevel.WRITE)
        try:
            assessment = self.review.attach_evidence(
                hypothesis_id, record_id, supports=bool(supports), source=self.source, note=note
            )
        except KnowledgeError as error:
            raise McpToolError(str(error)) from error
        payload = {
            "hypothesis_id": assessment.hypothesis_id,
            "status": assessment.status.value,
            "recommended": assessment.recommended.value,
            "supporting": assessment.supporting,
            "contradicting": assessment.contradicting,
            "rationale": assessment.rationale,
            "note": "Reaching supported is evidence; accepting it needs a person.",
        }
        return self._done("add_evidence", {"hypothesis_id": hypothesis_id, "record_id": record_id, "supports": supports}, payload)

    def assess(self, ids: Sequence[str], record: bool = False) -> dict[str, Any]:
        self._permit("assess", PermissionLevel.WRITE if record else PermissionLevel.READ)
        appraisals = self.appraiser.appraise_many(list(ids), record=bool(record))
        payload = {
            "contract": CONTRACT,
            "binding": False,
            "appraisals": [
                {
                    "id": item.record_id,
                    "rank": item.rank,
                    "score": item.score,
                    "basis": item.basis.value,
                    "sample": item.sample,
                    "favourable": item.favourable,
                    "unfavourable": item.unfavourable,
                    "rationale": item.rationale,
                    "method": item.method,
                }
                for item in appraisals
            ],
        }
        return self._done("assess", {"ids": list(ids), "record": record}, payload)

    def review_queue(self, topic: str | None = None, limit: int = 20) -> dict[str, Any]:
        self._permit("review_queue", PermissionLevel.READ)
        wanted = topic.strip().lower() if topic else None
        payload = {
            "awaiting_approval": [_summary(item) for item in self.review.pending(topic=wanted, limit=_bounded(limit))],
            "under_test": [_summary(item) for item in self.review.under_test(topic=wanted, limit=_bounded(limit))],
            "note": "Only a person can accept a hypothesis; /knowledge review does it in Iris.",
        }
        return self._done("review_queue", {"topic": topic}, payload)

    def explain(self, memory_id: str, max_depth: int = 3) -> dict[str, Any]:
        self._permit("explain", PermissionLevel.READ)
        try:
            rendered = self.review.explain(memory_id, max_depth=max(1, min(int(max_depth), 6)))
        except KnowledgeError as error:
            raise McpToolError(str(error)) from error
        return self._done("explain", {"memory_id": memory_id}, {"id": memory_id, "explanation": rendered})

    def _permit(self, name: str, permission: PermissionLevel) -> None:
        if self.permissions is None:
            return
        decision = self.permissions.enforce(
            PermissionRequest(tool=name, permission=permission, action=name, source=self.source)
        )
        if decision.decision is Decision.DENY:
            raise McpToolError(f"Not allowed: {decision.reason}.")
        if decision.decision is Decision.CONFIRM:
            raise McpToolError(
                f"{name} needs {permission.value} access, which this machine asks a person to confirm. "
                "Run it in Iris, where there is someone to ask."
            )

    def _done(self, name: str, arguments: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        if self.auditor is not None:
            try:
                self.auditor.record(
                    name,
                    arguments,
                    {"status": "success"},
                    source=self.source,
                    kind=ToolKind.MCP,
                )
            except Exception as error:
                logger.warning("Could not record an MCP call to %s: %s", name, error)
        return payload


def _bounded(limit: Any) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _topic(value: str) -> str:
    topic = str(value or "").strip().lower()
    if not topic:
        raise McpToolError("A record needs a topic, such as trading/candidates or iris/design")
    return topic


def _content(value: str) -> str:
    content = str(value or "").strip()
    if not content:
        raise McpToolError("A record needs content")
    return content


def _record(record: MemoryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind.value,
        "topic": record.topic,
        "status": record.status.value if record.status else None,
        "content": record.content,
        "source": record.source,
        "confidence": record.confidence,
        "occurred_at": record.occurred_at,
        "created_at": record.created_at,
    }


def _summary(item: Any) -> dict[str, Any]:
    return {
        "id": item.hypothesis.id,
        "topic": item.hypothesis.topic,
        "content": item.hypothesis.content,
        "status": item.hypothesis.status.value if item.hypothesis.status else None,
        "supporting": item.assessment.supporting,
        "contradicting": item.assessment.contradicting,
        "rationale": item.assessment.rationale,
    }


__all__ = ["IrisMcpTools", "McpToolError", "SOURCE"]
