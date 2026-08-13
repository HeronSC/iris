from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.assistant.protocols import ProposalStoreProtocol
from core.assistant.workflows import MemoryReviewWorkflow


class MemoryCommandHandler:
    def __init__(
        self,
        proposal_store: ProposalStoreProtocol,
        review_workflow: MemoryReviewWorkflow | None = None,
        output: OutputSink | None = None,
    ) -> None:
        self.proposal_store = proposal_store
        self.review_workflow = review_workflow
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        if not user_input.startswith("/memory"):
            return False

        parts = user_input.strip().split()
        if len(parts) == 1:
            emit_output(self.output, "Usage: /memory scan|review|list|show <proposal-id>|approve <proposal-id>|reject <proposal-id>")
            return True

        command = parts[1].lower()
        if command == "scan":
            if self.review_workflow is None:
                emit_output(self.output, "Scan workflow is not configured.")
                return True
            added, warning = self.review_workflow.scan_active_session()
            emit_output(self.output, f"Added {added} proposal(s).")
            if warning:
                emit_output(self.output, warning)
            return True

        if command == "list":
            proposals = self.proposal_store.list_pending()
            if not proposals:
                emit_output(self.output, "No pending proposals.")
                return True
            for proposal in proposals:
                emit_output(self.output, f"- {proposal.id} | {proposal.memory_area} | {proposal.target_id}")
            return True

        if command == "review":
            if self.review_workflow is None:
                emit_output(self.output, "Review workflow is not configured.")
                return True
            scan_added, warning = self.review_workflow.scan_active_session()
            if warning:
                emit_output(self.output, warning)
            total, approved, rejected = self.review_workflow.review_pending()
            if total == 0:
                emit_output(self.output, "No pending proposals.")
                return True
            emit_output(
                self.output,
                f"Reviewed {total} proposal(s). Approved: {approved}. Rejected: {rejected}."
                + (f" Added {scan_added} new proposal(s) from scan." if scan_added > 0 else "")
            )
            return True

        if command == "approve" or command == "reject":
            if len(parts) < 3:
                emit_output(self.output, f"Usage: /memory {command} <proposal-id>")
                return True
            if self.review_workflow is None:
                emit_output(self.output, "Review workflow is not configured.")
                return True
            ok, error = self.review_workflow.set_proposal_status(parts[2], approve=command == "approve")
            if not ok:
                emit_output(self.output, error)
                return True
            emit_output(self.output, f"{command.capitalize()}d proposal {parts[2]}.")
            return True

        if command == "show" and len(parts) > 2:
            proposal = self.proposal_store.get(parts[2])
            if proposal is None:
                emit_output(self.output, "Proposal not found.")
            else:
                emit_output(self.output, proposal.reason)
            return True

        emit_output(self.output, "Unknown /memory command.")
        return True

