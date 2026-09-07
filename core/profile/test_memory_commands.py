import tempfile
import unittest
from pathlib import Path

from core.assistant.memory_commands import MemoryCommandHandler
from core.assistant.workflows import MemoryReviewWorkflow
from core.profile.proposal import MemoryProposal
from core.profile.proposal_store import MemoryProposalStore


class MemoryCommandHandlerTests(unittest.TestCase):
    def test_memory_scan_command_adds_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            proposal_store = MemoryProposalStore(Path(tmpdir) / "proposals")
            proposal = MemoryProposal(
                id="p-scan-1",
                created_at="2026-01-01T00:00:00+00:00",
                status="pending",
                source_session_id="s-1",
                source_message_ids=["m-1"],
                memory_area="preferences",
                operation="add",
                target_id="coding-no-comments",
                reason="User preference",
                confidence=1.0,
                proposed_value={"id": "coding-no-comments", "name": "No comments", "value": True, "metadata": {"status": "active"}},
            )
            session = type("Session", (), {"id": "s-1", "get_messages": lambda self: [{"role": "user", "content": "Remember no comments"}]})()
            session_manager = type("SessionManager", (), {"get_active_session": lambda self: session})()
            generator = type(
                "Generator",
                (),
                {
                    "last_error": None,
                    "generate_proposals": lambda self, current_session, existing_memory: [proposal],
                },
            )()
            reviewer = type("Reviewer", (), {"review": lambda self, proposals: []})()
            update_service = type("UpdateService", (), {"apply_proposal": lambda self, proposal: {"status": "applied"}})()
            store = type("Store", (), {"to_dict": lambda self: {}})()
            workflow = MemoryReviewWorkflow(
                session_manager=session_manager,
                proposal_store=proposal_store,
                proposal_generator=generator,
                reviewer=reviewer,
                update_service=update_service,
                store=store,
            )
            handler = MemoryCommandHandler(proposal_store, review_workflow=workflow)

            handled = handler.handle("/memory scan", {"store": store})

            self.assertTrue(handled)
            self.assertEqual(len(proposal_store.list_pending()), 1)

    def test_memory_review_command_uses_pending_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            proposal_store = MemoryProposalStore(Path(tmpdir) / "proposals")
            proposal = MemoryProposal(
                id="p-1",
                created_at="2026-01-01T00:00:00+00:00",
                status="pending",
                source_session_id="s-1",
                source_message_ids=["m-1"],
                memory_area="preferences",
                operation="add",
                target_id="coding-no-comments",
                reason="User preference",
                confidence=1.0,
                proposed_value={"id": "coding-no-comments", "name": "No comments", "value": True, "metadata": {"status": "active"}},
            )
            proposal_store.add(proposal)

            applied = []
            reviewer = type("Reviewer", (), {"review": lambda self, proposals: [(proposals[0], "approve")]})()
            update_service = type("UpdateService", (), {"apply_proposal": lambda self, proposal: applied.append(proposal) or {"status": "applied"}})()
            session_manager = type("SessionManager", (), {"get_active_session": lambda self: None})()
            generator = type("Generator", (), {"last_error": None, "generate_proposals": lambda self, current_session, existing_memory: []})()
            store = type("Store", (), {"to_dict": lambda self: {}})()
            workflow = MemoryReviewWorkflow(
                session_manager=session_manager,
                proposal_store=proposal_store,
                proposal_generator=generator,
                reviewer=reviewer,
                update_service=update_service,
                store=store,
            )
            handler = MemoryCommandHandler(proposal_store, review_workflow=workflow)

            handled = handler.handle("/memory review", {})

            self.assertTrue(handled)
            self.assertEqual(len(applied), 1)


if __name__ == "__main__":
    unittest.main()

