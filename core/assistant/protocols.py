from __future__ import annotations

from typing import Any, Protocol

from core.memory.proposal import MemoryProposal


class SessionLike(Protocol):
    id: str | None
    title: str
    project_id: str | None
    summary: str
    metadata: dict[str, Any]

    def get_messages(self) -> list[dict[str, Any]]:
        ...


class SessionManagerProtocol(Protocol):
    def start_session(self, title: str | None = None, project_id: str | None = None) -> SessionLike:
        ...

    def resume_session(self, session_id: str) -> SessionLike:
        ...

    def add_message(self, role: str, content: str, metadata: dict[str, object] | None = None) -> None:
        ...

    def set_project(self, project_id: str | None) -> None:
        ...

    def close_active_session(self) -> None:
        ...

    def get_active_session(self) -> SessionLike | None:
        ...

    def save_active_session(self) -> None:
        ...

    def get_most_recent_session_id(self) -> str | None:
        ...


class SessionRepositoryProtocol(Protocol):
    def list_sessions(self, status: str | None = None, project_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        ...

    def save_session(self, session: SessionLike) -> None:
        ...


class ProposalStoreProtocol(Protocol):
    def add(self, proposal: MemoryProposal) -> None:
        ...

    def get(self, proposal_id: str) -> MemoryProposal | None:
        ...

    def list_pending(self) -> list[MemoryProposal]:
        ...

    def approve(self, proposal_id: str) -> MemoryProposal:
        ...

    def reject(self, proposal_id: str) -> MemoryProposal:
        ...


class ProposalGeneratorProtocol(Protocol):
    last_error: str | None

    def generate_proposals(self, session: SessionLike, existing_memory: dict[str, Any]) -> list[MemoryProposal]:
        ...


class ProposalReviewerProtocol(Protocol):
    def review(self, proposals: list[MemoryProposal]) -> list[tuple[MemoryProposal, str]]:
        ...


class MemoryUpdateServiceProtocol(Protocol):
    def apply_proposal(self, proposal: MemoryProposal) -> dict[str, Any]:
        ...


class MemoryStoreProtocol(Protocol):
    def to_dict(self) -> dict[str, Any]:
        ...
