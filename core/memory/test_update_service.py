import json
import tempfile
import unittest
from pathlib import Path

from core.assistant.proposal_commands import ProposalCommandHandler
from core.assistant.workflows import MemoryReviewWorkflow
from core.audit.logger import AuditLogger
from core.memory.proposal import MemoryProposal
from core.memory.proposal_reviewer import MemoryProposalReviewer
from core.memory.proposal_store import MemoryProposalStore
from core.memory.update_service import MemoryUpdateService


class UpdateServiceTests(unittest.TestCase):
    def test_proposal_create_generates_pending_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            proposal_store = MemoryProposalStore(Path(tmpdir) / "proposals")
            session = type("Session", (), {"id": "s-1", "get_messages": lambda self: [{"role": "user", "content": "Remember that I prefer no comments"}]})()
            session_manager = type("SessionManager", (), {"get_active_session": lambda self: session})()
            generator = type("Generator", (), {"generate_proposals": lambda self, session, existing_memory: [MemoryProposal(id="p-1", created_at="2026-01-01T00:00:00+00:00", status="pending", source_session_id="s-1", source_message_ids=["m-1"], memory_area="preferences", operation="add", target_id="coding-no-comments", reason="User preference", confidence=1.0, proposed_value={"id": "coding-no-comments", "name": "No comments", "value": True, "metadata": {"status": "active"}})]})()
            handler = ProposalCommandHandler(proposal_store, session_manager=session_manager, proposal_generator=generator)

            handled = handler.handle("/proposal create", {"store": None})

            self.assertTrue(handled)
            self.assertEqual(len(proposal_store.list_pending()), 1)

    def test_apply_proposal_updates_memory_and_logs_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_path = Path(tmpdir) / "memory"
            memory_path.mkdir()
            (memory_path / "profile.json").write_text(json.dumps({"profile": {}}), encoding="utf-8")
            (memory_path / "preferences.json").write_text(json.dumps({"preferences": []}), encoding="utf-8")
            (memory_path / "projects.json").write_text(json.dumps({"projects": []}), encoding="utf-8")
            (memory_path / "knowledge.json").write_text(json.dumps({"knowledge_areas": []}), encoding="utf-8")
            proposal_store = MemoryProposalStore(Path(tmpdir) / "proposals")
            audit_logger = AuditLogger(Path(tmpdir) / "audit")
            service = MemoryUpdateService(memory_path, proposal_store, audit_logger)
            proposal = MemoryProposal(
                id="proposal-1",
                created_at="2026-01-01T00:00:00+00:00",
                status="approved",
                source_session_id="session-1",
                source_message_ids=["msg-1"],
                memory_area="preferences",
                operation="add",
                target_id="coding-no-comments",
                reason="User preference",
                confidence=1.0,
                proposed_value={"id": "coding-no-comments", "name": "No comments", "value": True, "metadata": {"status": "active"}},
            )
            proposal_store.add(proposal)

            result = service.apply_proposal(proposal)

            self.assertEqual(result["status"], "applied")
            written = json.loads((memory_path / "preferences.json").read_text(encoding="utf-8"))
            self.assertEqual(written["preferences"][0]["id"], "coding-no-comments")
            self.assertTrue(audit_logger.read_entries())

    def test_apply_proposal_logs_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_path = Path(tmpdir) / "memory"
            memory_path.mkdir()
            (memory_path / "profile.json").write_text(json.dumps({"profile": {}}), encoding="utf-8")
            (memory_path / "preferences.json").write_text(json.dumps({"preferences": []}), encoding="utf-8")
            (memory_path / "projects.json").write_text(json.dumps({"projects": []}), encoding="utf-8")
            (memory_path / "knowledge.json").write_text(json.dumps({"knowledge_areas": []}), encoding="utf-8")
            proposal_store = MemoryProposalStore(Path(tmpdir) / "proposals")
            audit_logger = AuditLogger(Path(tmpdir) / "audit")
            service = MemoryUpdateService(memory_path, proposal_store, audit_logger)
            proposal = MemoryProposal(
                id="proposal-fail-1",
                created_at="2026-01-01T00:00:00+00:00",
                status="approved",
                source_session_id="session-1",
                source_message_ids=["msg-1"],
                memory_area="preferences",
                operation="add",
                target_id="coding-no-comments",
                reason="User preference",
                confidence=1.0,
                proposed_value={"id": "coding-no-comments", "name": "No comments", "value": True, "metadata": {"status": "active"}},
            )
            proposal_store.add(proposal)
            duplicate = MemoryProposal(
                id="proposal-fail-2",
                created_at="2026-01-01T00:00:00+00:00",
                status="approved",
                source_session_id="session-1",
                source_message_ids=["msg-2"],
                memory_area="preferences",
                operation="add",
                target_id="coding-no-comments",
                reason="Duplicate preference",
                confidence=1.0,
                proposed_value={"id": "coding-no-comments", "name": "No comments", "value": True, "metadata": {"status": "active"}},
            )

            service.apply_proposal(proposal)
            with self.assertRaises(Exception):
                service.apply_proposal(duplicate)

            entries = audit_logger.read_entries()
            self.assertEqual(entries[-1]["action"], "failed")
            self.assertEqual(entries[-1]["proposal_id"], "proposal-fail-2")

    def test_reviewer_returns_decisions_without_mutating_store(self) -> None:
        proposal = MemoryProposal(
            id="proposal-2",
            created_at="2026-01-01T00:00:00+00:00",
            status="pending",
            source_session_id="session-1",
            source_message_ids=["msg-1"],
            memory_area="preferences",
            operation="add",
            target_id="coding-style",
            reason="User preference",
            confidence=1.0,
            proposed_value={"id": "coding-style", "name": "Prefer explicit styles", "value": True, "metadata": {"status": "active"}},
        )
        reviewer = MemoryProposalReviewer()

        decisions = reviewer.review([proposal], input_func=lambda _prompt: "y")

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0][1], "approved")

    def test_pending_proposal_is_applied_then_marked_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_path = Path(tmpdir) / "memory"
            memory_path.mkdir()
            (memory_path / "profile.json").write_text(json.dumps({"profile": {}}), encoding="utf-8")
            (memory_path / "preferences.json").write_text(json.dumps({"preferences": []}), encoding="utf-8")
            (memory_path / "projects.json").write_text(json.dumps({"projects": []}), encoding="utf-8")
            (memory_path / "knowledge.json").write_text(json.dumps({"knowledge_areas": []}), encoding="utf-8")

            proposal_store = MemoryProposalStore(Path(tmpdir) / "proposals")
            audit_logger = AuditLogger(Path(tmpdir) / "audit")
            update_service = MemoryUpdateService(memory_path, proposal_store, audit_logger)

            proposal = MemoryProposal(
                id="proposal-integration-1",
                created_at="2026-01-01T00:00:00+00:00",
                status="pending",
                source_session_id="session-1",
                source_message_ids=["msg-1"],
                memory_area="preferences",
                operation="add",
                target_id="coding-no-comments",
                reason="User preference",
                confidence=1.0,
                proposed_value={"id": "coding-no-comments", "name": "No comments", "value": True, "metadata": {"status": "active"}},
            )
            proposal_store.add(proposal)

            session_manager = type("SessionManager", (), {"get_active_session": lambda self: None})()
            generator = type("Generator", (), {"last_error": None, "generate_proposals": lambda self, current_session, existing_memory: []})()
            reviewer = type("Reviewer", (), {"review": lambda self, proposals: [(proposals[0], "approve")]})()
            store = type("Store", (), {"to_dict": lambda self: {}})()
            workflow = MemoryReviewWorkflow(
                session_manager=session_manager,
                proposal_store=proposal_store,
                proposal_generator=generator,
                reviewer=reviewer,
                update_service=update_service,
                store=store,
            )

            total, approved, rejected = workflow.review_pending()

            self.assertEqual(total, 1)
            self.assertEqual(approved, 1)
            self.assertEqual(rejected, 0)
            written = json.loads((memory_path / "preferences.json").read_text(encoding="utf-8"))
            self.assertEqual(written["preferences"][0]["id"], "coding-no-comments")
            stored = proposal_store.get("proposal-integration-1")
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "approved")
            entries = audit_logger.read_entries()
            self.assertTrue(entries)
            self.assertEqual(entries[-1]["action"], "approved")

    def test_pending_proposal_remains_pending_when_apply_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_path = Path(tmpdir) / "memory"
            memory_path.mkdir()
            (memory_path / "profile.json").write_text(json.dumps({"profile": {}}), encoding="utf-8")
            (memory_path / "preferences.json").write_text(json.dumps({"preferences": []}), encoding="utf-8")
            (memory_path / "projects.json").write_text(json.dumps({"projects": []}), encoding="utf-8")
            (memory_path / "knowledge.json").write_text(json.dumps({"knowledge_areas": []}), encoding="utf-8")

            proposal_store = MemoryProposalStore(Path(tmpdir) / "proposals")
            audit_logger = AuditLogger(Path(tmpdir) / "audit")
            update_service = MemoryUpdateService(memory_path, proposal_store, audit_logger)

            proposal = MemoryProposal(
                id="proposal-integration-fail-1",
                created_at="2026-01-01T00:00:00+00:00",
                status="pending",
                source_session_id="session-1",
                source_message_ids=["msg-1"],
                memory_area="preferences",
                operation="update",
                target_id="missing-preference",
                reason="Should fail due to missing target",
                confidence=1.0,
                proposed_value={"id": "missing-preference", "name": "Missing", "value": True, "metadata": {"status": "active"}},
            )
            proposal_store.add(proposal)

            session_manager = type("SessionManager", (), {"get_active_session": lambda self: None})()
            generator = type("Generator", (), {"last_error": None, "generate_proposals": lambda self, current_session, existing_memory: []})()
            reviewer = type("Reviewer", (), {"review": lambda self, proposals: [(proposals[0], "approve")]})()
            store = type("Store", (), {"to_dict": lambda self: {}})()
            workflow = MemoryReviewWorkflow(
                session_manager=session_manager,
                proposal_store=proposal_store,
                proposal_generator=generator,
                reviewer=reviewer,
                update_service=update_service,
                store=store,
            )

            total, approved, rejected = workflow.review_pending()

            self.assertEqual(total, 1)
            self.assertEqual(approved, 0)
            self.assertEqual(rejected, 0)
            stored = proposal_store.get("proposal-integration-fail-1")
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "pending")
            entries = audit_logger.read_entries()
            self.assertTrue(entries)
            self.assertEqual(entries[-1]["action"], "failed")


if __name__ == "__main__":
    unittest.main()

