from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.assistant.protocols import ProposalGeneratorProtocol, ProposalStoreProtocol, SessionManagerProtocol


class ProposalCommandHandler:
    def __init__(
        self,
        proposal_store: ProposalStoreProtocol,
        session_manager: SessionManagerProtocol | None = None,
        proposal_generator: ProposalGeneratorProtocol | None = None,
        output: OutputSink | None = None,
    ) -> None:
        self.proposal_store = proposal_store
        self.session_manager = session_manager
        self.proposal_generator = proposal_generator
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        if not user_input.startswith("/proposal"):
            return False

        parts = user_input.strip().split()
        if len(parts) == 1:
            emit_output(self.output, "Usage: /proposal create")
            return True

        command = parts[1].lower()
        if command == "create":
            if self.proposal_generator is None or self.session_manager is None:
                emit_output(self.output, "Proposal generation is not configured.")
                return True
            session = self.session_manager.get_active_session()
            if session is None:
                emit_output(self.output, "No active session.")
                return True
            store = state.get("store")
            memory_snapshot = store.to_dict() if store is not None and hasattr(store, "to_dict") else {}
            proposals = self.proposal_generator.generate_proposals(session, memory_snapshot)
            for proposal in proposals:
                self.proposal_store.add(proposal)
            emit_output(self.output, f"Created {len(proposals)} proposal(s).")
            return True

        emit_output(self.output, "Unknown /proposal command.")
        return True

