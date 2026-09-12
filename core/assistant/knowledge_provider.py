# File: core/assistant/knowledge_provider.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.assistant.general_knowledge_router import (
    CapabilityDefinition,
    GeneralKnowledgeResult,
    KnowledgeProvider,
    ProviderExecutionError,
)
from core.knowledge import KnowledgeQuery, KnowledgeRetriever, MemoryKind, MemoryStatus

_KINDS = {kind.value: kind for kind in MemoryKind}

_DEFAULT_TOKEN_BUDGET = 700


@dataclass(frozen=True)
class RecallRequest:
    query: str
    topic: str | None = None
    kinds: tuple[MemoryKind, ...] = ()
    limit: int = 6
    include_rejected: bool = False


class KnowledgeRecallProvider(KnowledgeProvider):

    name = "recall"

    def __init__(self, retriever: KnowledgeRetriever, *, token_budget: int = _DEFAULT_TOKEN_BUDGET, scopes: Any = None) -> None:
        self.retriever = retriever
        self.token_budget = token_budget
        self.scopes = scopes

    def _visible_scopes(self) -> tuple[str, ...]:
        provider = self.scopes
        if provider is None:
            return ()
        try:
            found = provider() if callable(provider) else provider
        except Exception:
            return ()
        return tuple(str(item) for item in (found or ()))

    def can_handle(self, text: str) -> bool:
        return False

    def definition(self) -> CapabilityDefinition:
        return CapabilityDefinition(
            name=self.name,
            description=(
                "Use to recall what Iris has previously observed, decided, learned or is "
                "currently testing, before answering from general knowledge. Good for "
                "questions about past results, prior decisions, and why something is believed."
            ),
            request_schema={
                "query": "string",
                "topic": "string|null",
                "kinds": "array of observation|outcome|decision|hypothesis|knowledge|fact",
                "limit": "integer",
            },
        )

    def parse_request(self, payload: dict[str, Any]) -> RecallRequest | None:
        query = str(payload.get("query", "")).strip()
        if not query:
            return None
        topic_value = payload.get("topic")
        topic = str(topic_value).strip() or None if topic_value is not None else None

        raw_kinds = payload.get("kinds")
        kinds: list[MemoryKind] = []
        if isinstance(raw_kinds, list):
            for item in raw_kinds:
                kind = _KINDS.get(str(item).strip().lower())
                if kind is not None:
                    kinds.append(kind)
        try:
            limit = max(1, min(20, int(payload.get("limit", 6))))
        except (TypeError, ValueError):
            limit = 6
        return RecallRequest(query=query, topic=topic, kinds=tuple(kinds), limit=limit)

    def execute_request(self, request_obj: Any) -> str:
        detailed = self.execute_detailed_request(request_obj)
        if detailed is None:
            raise ProviderExecutionError("Nothing recalled", unavailable=False)
        return str(detailed.response or "").strip()

    def execute_detailed_request(self, request_obj: Any) -> GeneralKnowledgeResult | None:
        if not isinstance(request_obj, RecallRequest):
            raise ProviderExecutionError("recall requires a RecallRequest", unavailable=False)

        statuses: tuple[MemoryStatus, ...] = ()
        if not request_obj.include_rejected:
            statuses = tuple(s for s in MemoryStatus if s not in {MemoryStatus.REJECTED, MemoryStatus.SUPERSEDED, MemoryStatus.RETIRED})

        result = self.retriever.retrieve(
            KnowledgeQuery(
                text=request_obj.query,
                topic=request_obj.topic,
                kinds=request_obj.kinds,
                statuses=statuses,
                limit=request_obj.limit,
                max_tokens=self.token_budget,
                scopes=self._visible_scopes(),
            )
        )

        if not result.records:
            return GeneralKnowledgeResult(
                provider=self.name,
                response=f"Nothing recorded about '{request_obj.query}' yet.",
                detail_type="markdown",
                detail_title="Recall",
                detail_content=f"## Recall\n\nNothing recorded about '{request_obj.query}' yet.",
                metadata={"capability": self.name, "found": 0, "diagnostics": result.diagnostics},
            )

        lines = [
            f"- {item.record.content} [{item.record.kind.value}, {item.record.status.value}]"
            for item in result.records
        ]
        response = "Recalled:\n" + "\n".join(lines)
        detail = "\n".join(
            [f"## Recall: {request_obj.query}", ""]
            + [
                f"- **{item.record.kind.value}** ({item.record.status.value}) "
                f"{item.record.content}  \n  _{item.record.topic} · {item.record.source}_"
                for item in result.records
            ]
        )
        if result.diagnostics.get("candidate_limit_reached"):
            detail += "\n\n_More records matched than were examined; narrow the topic to see older ones._"

        return GeneralKnowledgeResult(
            provider=self.name,
            response=response,
            detail_type="markdown",
            detail_title=f"Recall: {request_obj.query}"[:60],
            detail_content=detail,
            metadata={
                "capability": self.name,
                "found": len(result.records),
                "diagnostics": result.diagnostics,
            },
        )
