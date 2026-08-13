from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class ProposalValidationError(ValueError):
    pass


@dataclass
class MemoryProposal:
    id: str
    created_at: str
    status: str
    source_session_id: str | None
    source_message_ids: list[str]
    memory_area: str
    operation: str
    target_id: str
    reason: str
    confidence: float
    proposed_value: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MemoryProposal":
        try:
            return cls(
                id=str(payload["id"]),
                created_at=str(payload.get("created_at") or cls._utc_now_iso()),
                status=str(payload.get("status", "pending")),
                source_session_id=payload.get("source_session_id"),
                source_message_ids=[str(item) for item in payload.get("source_message_ids", [])],
                memory_area=str(payload["memory_area"]),
                operation=str(payload["operation"]),
                target_id=str(payload["target_id"]),
                reason=str(payload.get("reason", "")),
                confidence=float(payload.get("confidence", 0.0)),
                proposed_value=dict(payload.get("proposed_value", {})),
            )
        except KeyError as error:
            raise ProposalValidationError(f"Missing proposal field: {error}") from error

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "status": self.status,
            "source_session_id": self.source_session_id,
            "source_message_ids": self.source_message_ids,
            "memory_area": self.memory_area,
            "operation": self.operation,
            "target_id": self.target_id,
            "reason": self.reason,
            "confidence": self.confidence,
            "proposed_value": self.proposed_value,
        }

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
