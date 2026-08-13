from __future__ import annotations

from typing import Any

from core.assistant.protocols import (
    MemoryUpdateServiceProtocol,
    ProposalGeneratorProtocol,
    ProposalReviewerProtocol,
    ProposalStoreProtocol,
    SessionLike,
    SessionManagerProtocol,
)
from core.memory.proposal_store import ProposalStoreError


class SessionCloseWorkflow:
    def __init__(
        self,
        session_manager: SessionManagerProtocol,
        proposal_store: ProposalStoreProtocol,
        proposal_generator: ProposalGeneratorProtocol,
        config: dict[str, Any],
        store: Any,
    ) -> None:
        self.session_manager = session_manager
        self.proposal_store = proposal_store
        self.proposal_generator = proposal_generator
        self.config = config
        self.store = store

    def close_active_session_with_scan(self, closing_session: SessionLike) -> tuple[int, str | None]:
        memory_updates = self.config.get("memory_updates", {}) if isinstance(self.config, dict) else {}
        scan_on_close = bool(memory_updates.get("scan_on_session_close", False)) if isinstance(memory_updates, dict) else False
        if not scan_on_close:
            self.session_manager.close_active_session()
            return 0, None

        proposals = self.proposal_generator.generate_proposals(closing_session, self.store.to_dict())
        for proposal in proposals:
            self.proposal_store.add(proposal)

        self.session_manager.close_active_session()

        if self.proposal_generator.last_error:
            return len(proposals), f"Scan warning: {self.proposal_generator.last_error}"
        return len(proposals), None


class MemoryReviewWorkflow:
    def __init__(
        self,
        session_manager: SessionManagerProtocol,
        proposal_store: ProposalStoreProtocol,
        proposal_generator: ProposalGeneratorProtocol,
        reviewer: ProposalReviewerProtocol,
        update_service: MemoryUpdateServiceProtocol,
        store: Any,
    ) -> None:
        self.session_manager = session_manager
        self.proposal_store = proposal_store
        self.proposal_generator = proposal_generator
        self.reviewer = reviewer
        self.update_service = update_service
        self.store = store

    def scan_active_session(self) -> tuple[int, str | None]:
        session = self.session_manager.get_active_session()
        if session is None:
            return 0, "No active session to scan."

        proposals = self.proposal_generator.generate_proposals(session, self.store.to_dict())
        for proposal in proposals:
            self.proposal_store.add(proposal)

        warning = None
        if self.proposal_generator.last_error:
            warning = f"Scan warning: {self.proposal_generator.last_error}"
        return len(proposals), warning

    def review_pending(self) -> tuple[int, int, int]:
        pending = self.proposal_store.list_pending()
        if not pending:
            return 0, 0, 0

        decisions = self.reviewer.review(pending)
        approved_count = 0
        rejected_count = 0

        for proposal, decision in decisions:
            normalized_decision = decision.strip().lower()
            if normalized_decision in {"approve", "approved"}:
                try:
                    self.update_service.apply_proposal(proposal)
                except Exception:
                    continue
                self.proposal_store.approve(proposal.id)
                approved_count = approved_count + 1
            else:
                self.proposal_store.reject(proposal.id)
                rejected_count = rejected_count + 1

        return len(pending), approved_count, rejected_count

    def set_proposal_status(self, proposal_id: str, approve: bool) -> tuple[bool, str]:
        try:
            if approve:
                proposal = self.proposal_store.get(proposal_id)
                if proposal is None:
                    return False, f"Proposal not found: {proposal_id}"
                self.update_service.apply_proposal(proposal)
                self.proposal_store.approve(proposal_id)
                return True, ""

            self.proposal_store.reject(proposal_id)
            return True, ""
        except ProposalStoreError as error:
            return False, str(error)
        except Exception as error:
            if approve:
                return False, f"Could not apply proposal {proposal_id}: {error}"
            return False, str(error)

