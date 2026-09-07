# File: core/tests/test_knowledge_review.py

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.assistant.knowledge_commands import KnowledgeCommandHandler
from core.audit.logger import AuditLogger
from core.knowledge import KnowledgeError, KnowledgeGraph, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.review import KnowledgeReviewWorkflow
from core.storage.sqlite_database import SQLiteDatabase

TOPIC = "trading/candidates"


class _Sink:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str | None]] = []

    def __call__(self, text: str, role: str | None = None) -> None:
        self.lines.append((text, role))

    @property
    def text(self) -> str:
        return "\n".join(line for line, _ in self.lines)

    @property
    def errors(self) -> str:
        return "\n".join(line for line, role in self.lines if role == "error")


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.graph = KnowledgeGraph(SQLiteDatabase(root / "knowledge.db"))
        self.tracker = HypothesisTracker(self.graph)
        self.audit = AuditLogger(root / "audit")
        self.workflow = KnowledgeReviewWorkflow(self.tracker, audit_logger=self.audit)
        self.hypothesis = self.tracker.propose("Volume predicts outperformance.", topic=TOPIC, source="analysis")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _support(self, supporting: int = 5, contradicting: int = 0) -> None:
        for index in range(supporting):
            self.graph.add_evidence(
                self.hypothesis.id,
                MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, source="broker", content=f"win {index}"),
                supports=True,
            )
        for index in range(contradicting):
            self.graph.add_evidence(
                self.hypothesis.id,
                MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, source="broker", content=f"loss {index}"),
                supports=False,
            )

    def test_refresh_brings_the_queue_up_to_date(self) -> None:
        """Evidence arrives without asking a hypothesis to reconsider."""
        self._support()
        self.assertEqual(self.workflow.pending(), [])

        moved = self.workflow.refresh()

        self.assertEqual(moved, 1)
        self.assertEqual([item.hypothesis.id for item in self.workflow.pending()], [self.hypothesis.id])

    def test_a_pending_entry_explains_itself(self) -> None:
        self._support()
        self.workflow.refresh()

        summary = self.workflow.pending()[0].summary()

        self.assertIn("Volume predicts outperformance.", summary)
        self.assertIn("5 of 5", summary)

    def test_approving_records_who_and_writes_the_audit_trail(self) -> None:
        self._support()
        self.workflow.refresh()

        self.workflow.approve(self.hypothesis.id, approved_by="henry", note="For candidate ranking.")

        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.ACCEPTED)
        entries = self.audit.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "approved")
        self.assertEqual(entries[0]["actor"], "henry")
        self.assertEqual(entries[0]["supporting"], 5)
        self.assertEqual(entries[0]["hypothesis_id"], self.hypothesis.id)

    def test_declining_keeps_the_reason(self) -> None:
        """Evidence can hold and a person can still not want the behaviour."""
        self._support()
        self.workflow.refresh()

        self.workflow.decline(self.hypothesis.id, declined_by="henry", reason="Too risky before open.")

        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.REJECTED)
        decision = self.graph.records.list_by_topic(TOPIC, kind=MemoryKind.DECISION)[0]
        self.assertIn("Too risky before open.", decision.content)
        self.assertEqual(decision.data["declined_by"], "henry")
        entries = self.audit.read_entries()
        self.assertEqual(entries[0]["action"], "declined")
        self.assertEqual(entries[0]["detail"], "Too risky before open.")

    def test_declining_demands_a_reason(self) -> None:
        self._support()
        self.workflow.refresh()

        with self.assertRaises(KnowledgeError):
            self.workflow.decline(self.hypothesis.id, declined_by="henry", reason="  ")

    def test_a_declined_hypothesis_leaves_the_queue(self) -> None:
        self._support()
        self.workflow.refresh()
        self.workflow.decline(self.hypothesis.id, declined_by="henry", reason="No.")

        self.assertEqual(self.workflow.pending(), [])
        self.assertEqual(self.workflow.refresh(), 0, "a settled hypothesis must not come back")

    def test_ids_can_be_typed_short(self) -> None:
        self.assertEqual(self.workflow.find(self.hypothesis.id[:8]).id, self.hypothesis.id)
        self.assertIsNone(self.workflow.find("zzzz"))
        self.assertIsNone(self.workflow.find("ab"), "too short to disambiguate")

    def test_the_workflow_runs_without_an_audit_logger(self) -> None:
        workflow = KnowledgeReviewWorkflow(self.tracker)
        self._support()
        workflow.refresh()

        workflow.approve(self.hypothesis.id, approved_by="henry")

        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.ACCEPTED)


class KnowledgeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.graph = KnowledgeGraph(SQLiteDatabase(root / "knowledge.db"))
        self.tracker = HypothesisTracker(self.graph)
        self.workflow = KnowledgeReviewWorkflow(self.tracker, audit_logger=AuditLogger(root / "audit"))
        self.sink = _Sink()
        self.handler = KnowledgeCommandHandler(self.workflow, output=self.sink, actor="henry")
        self.hypothesis = self.tracker.propose("Volume predicts outperformance.", topic=TOPIC, source="analysis")
        for index in range(5):
            self.graph.add_evidence(
                self.hypothesis.id,
                MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, source="broker", content=f"win {index}"),
                supports=True,
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_other_commands_are_left_alone(self) -> None:
        self.assertFalse(self.handler.handle("/memory review", {}))
        self.assertFalse(self.handler.handle("hello", {}))

    def test_review_re_assesses_then_lists(self) -> None:
        self.assertTrue(self.handler.handle("/knowledge review", {}))

        self.assertIn("changed status", self.sink.text)
        self.assertIn("Volume predicts outperformance.", self.sink.text)
        self.assertIn("awaiting approval", self.sink.text)

    def test_pending_does_not_re_assess(self) -> None:
        self.handler.handle("/knowledge pending", {})

        self.assertIn("Nothing is waiting", self.sink.text)

    def test_approving_from_the_command_line(self) -> None:
        self.handler.handle("/knowledge review", {})
        self.handler.handle(f"/knowledge approve {self.hypothesis.id[:8]} for candidate ranking", {})

        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.ACCEPTED)
        self.assertIn("Approved:", self.sink.text)

    def test_declining_needs_a_reason(self) -> None:
        self.handler.handle("/knowledge review", {})
        self.sink.lines.clear()

        self.handler.handle(f"/knowledge decline {self.hypothesis.id[:8]}", {})

        self.assertIn("needs a reason", self.sink.errors)
        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.SUPPORTED)

    def test_declining_with_a_reason(self) -> None:
        self.handler.handle("/knowledge review", {})
        self.handler.handle(f"/knowledge decline {self.hypothesis.id[:8]} too risky before open", {})

        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.REJECTED)

    def test_why_renders_the_provenance(self) -> None:
        self.handler.handle(f"/knowledge why {self.hypothesis.id[:8]}", {})

        self.assertIn("Volume predicts outperformance.", self.sink.text)
        self.assertIn("supported by", self.sink.text)

    def test_an_unknown_id_is_reported_not_ignored(self) -> None:
        self.handler.handle("/knowledge approve zzzzzzzz", {})

        self.assertIn("Nothing matches", self.sink.errors)

    def test_bare_command_shows_usage(self) -> None:
        self.handler.handle("/knowledge", {})
        self.assertIn("Usage:", self.sink.text)

    def test_topics_counts_what_is_known(self) -> None:
        self.handler.handle("/knowledge topics", {})
        self.assertIn(f"- {TOPIC}: 1 hypothesis(es)", self.sink.text)

    def test_domain_errors_surface_as_errors(self) -> None:
        """Approving something not yet supported should say so, not crash."""
        self.handler.handle(f"/knowledge approve {self.hypothesis.id[:8]}", {})

        self.assertIn("Only a supported hypothesis", self.sink.errors)


class RecordingCommandTests(unittest.TestCase):
    """Stage one: the plainest way to get something into the store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.graph = KnowledgeGraph(SQLiteDatabase(root / "knowledge.db"))
        self.tracker = HypothesisTracker(self.graph)
        self.workflow = KnowledgeReviewWorkflow(self.tracker, audit_logger=AuditLogger(root / "audit"))
        self.sink = _Sink()
        self.handler = KnowledgeCommandHandler(self.workflow, output=self.sink, actor="henry")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _observe(self, text: str = "/knowledge observe iris/performance indexing 4200 documents took 90 seconds"):
        self.handler.handle(text, {})
        return self.graph.records.list_by_topic(text.split()[2], kind=MemoryKind.OBSERVATION)[0]

    def test_observing_records_what_was_seen(self) -> None:
        record = self._observe()

        self.assertEqual(record.kind, MemoryKind.OBSERVATION)
        self.assertEqual(record.topic, "iris/performance")
        self.assertEqual(record.content, "indexing 4200 documents took 90 seconds")
        self.assertEqual(record.status, MemoryStatus.OBSERVED)

    def test_a_typed_observation_records_who_typed_it(self) -> None:
        """Provenance separates what a person said from what a feed reported."""
        self.assertEqual(self._observe().source, "user:henry")

    def test_the_reply_says_how_to_close_it(self) -> None:
        record = self._observe()

        self.assertIn(f"Recorded {record.id[:8]}", self.sink.text)
        self.assertIn(f"/knowledge outcome {record.id[:8]}", self.sink.text)

    def test_a_flat_topic_earns_a_nudge_not_a_refusal(self) -> None:
        self.handler.handle("/knowledge observe scratch something happened", {})

        self.assertIn("domain/subtopic", self.sink.text)
        self.assertEqual(len(self.graph.records.list_by_topic("scratch")), 1, "the nudge must not block the write")

    def test_topics_are_normalised(self) -> None:
        self.handler.handle("/knowledge observe Iris/Performance a thing", {})

        self.assertEqual(len(self.graph.records.list_by_topic("iris/performance")), 1)

    def test_observing_needs_a_topic_and_content(self) -> None:
        self.handler.handle("/knowledge observe", {})
        self.handler.handle("/knowledge observe iris/performance", {})

        self.assertEqual(self.graph.records.count(), 0)
        self.assertIn("Usage:", self.sink.errors)

    def test_an_outcome_closes_the_observation(self) -> None:
        record = self._observe()
        self.sink.lines.clear()

        self.handler.handle(f"/knowledge outcome {record.id[:8]} the index rebuild fixed it", {})

        outcome = self.graph.outcome_for(record.id)
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.content, "the index rebuild fixed it")
        self.assertEqual(outcome.topic, "iris/performance", "an outcome inherits its observation's topic")
        self.assertIn("as the outcome of", self.sink.text)

    def test_only_an_observation_can_be_closed(self) -> None:
        hypothesis = self.tracker.propose("An idea.", topic="iris/performance", source="analysis")

        self.handler.handle(f"/knowledge outcome {hypothesis.id[:8]} something", {})

        self.assertIn("that is a hypothesis", self.sink.errors)

    def test_an_outcome_needs_something_to_say(self) -> None:
        record = self._observe()
        self.sink.lines.clear()

        self.handler.handle(f"/knowledge outcome {record.id[:8]}", {})

        self.assertIsNone(self.graph.outcome_for(record.id))
        self.assertIn("Usage:", self.sink.errors)

    def test_open_lists_what_still_needs_an_outcome(self) -> None:
        first = self._observe()
        self.handler.handle("/knowledge observe iris/routing the planner chose recall", {})
        self.handler.handle(f"/knowledge outcome {first.id[:8]} resolved", {})
        self.sink.lines.clear()

        self.handler.handle("/knowledge open", {})

        self.assertIn("the planner chose recall", self.sink.text)
        self.assertNotIn("indexing 4200", self.sink.text)

    def test_open_can_be_narrowed_to_a_topic(self) -> None:
        self._observe()
        self.handler.handle("/knowledge observe iris/routing the planner chose recall", {})
        self.sink.lines.clear()

        self.handler.handle("/knowledge open iris/routing", {})

        self.assertIn("the planner chose recall", self.sink.text)
        self.assertNotIn("indexing 4200", self.sink.text)

    def test_open_says_so_when_nothing_is_waiting(self) -> None:
        self.handler.handle("/knowledge open", {})
        self.assertIn("No observations are waiting", self.sink.text)

    def test_an_ambiguous_id_asks_for_more_rather_than_guessing(self) -> None:
        from core.knowledge import MemoryRecord as Record

        for suffix in ("aaaa1111", "aaaa2222"):
            self.graph.records.add(Record(id="dead" + suffix, kind=MemoryKind.OBSERVATION,
                                          topic="iris/performance", source="user:henry", content=f"one {suffix}"))

        self.handler.handle("/knowledge outcome dead something", {})

        self.assertIn("ambiguous", self.sink.errors)

    def test_an_observation_can_be_asked_about_by_id(self) -> None:
        """find() used to search hypotheses only, so this was unreachable."""
        record = self._observe()
        self.sink.lines.clear()

        self.handler.handle(f"/knowledge why {record.id[:8]}", {})

        self.assertIn("indexing 4200 documents", self.sink.text)


class ApplicationRoutingTests(unittest.TestCase):
    """The handler is only useful if /knowledge reaches it and the reply is visible."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.graph = KnowledgeGraph(SQLiteDatabase(root / "knowledge.db"))
        self.tracker = HypothesisTracker(self.graph)
        self.workflow = KnowledgeReviewWorkflow(self.tracker, audit_logger=AuditLogger(root / "audit"))
        self.hypothesis = self.tracker.propose("Volume predicts outperformance.", topic=TOPIC, source="analysis")
        for index in range(5):
            self.graph.add_evidence(
                self.hypothesis.id,
                MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, source="broker", content=f"win {index}"),
                supports=True,
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _app(self):
        from core.application.service import IrisApplication

        app = IrisApplication()
        app.initialized = True
        app.state = {"assistant_name": "Iris"}
        app.knowledge = self.graph
        app.hypotheses = self.tracker
        app.knowledge_review = self.workflow
        app.knowledge_handler = KnowledgeCommandHandler(self.workflow, output=app._sink, actor="henry")

        class _Executor:
            def has_pending_confirmation(self) -> bool:
                return False

            def pending_confirmation_preview(self):
                return None

        class _False:
            def handle(self, *_args, **_kwargs) -> bool:
                return False

            def handle_natural_language(self, *_args, **_kwargs) -> bool:
                return False

        app.action_executor = _Executor()
        app.pending_action_manager = _False()
        for name in ("save_handler", "session_handler", "topic_handler", "proposal_handler",
                     "memory_handler", "index_handler", "search_handler", "action_handler",
                     "project_handler", "intent_router"):
            setattr(app, name, _False())
        return app

    def test_the_command_reaches_the_handler_and_the_reply_is_rendered(self) -> None:
        app = self._app()

        response = app.process_message("/knowledge review")

        rendered = "\n".join(str(item["text"]) for item in response.details.items)
        self.assertIn("awaiting approval", rendered)
        self.assertIn("Volume predicts outperformance.", rendered)

    def test_approving_through_the_application_changes_the_record(self) -> None:
        app = self._app()
        app.process_message("/knowledge review")

        app.process_message(f"/knowledge approve {self.hypothesis.id[:8]}")

        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.ACCEPTED)


if __name__ == "__main__":
    unittest.main()


class HypothesisCommandTests(unittest.TestCase):
    """Stage two: the rest of the loop, reachable without opening Python."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.graph = KnowledgeGraph(SQLiteDatabase(root / "knowledge.db"))
        self.tracker = HypothesisTracker(self.graph)
        self.workflow = KnowledgeReviewWorkflow(self.tracker, audit_logger=AuditLogger(root / "audit"))
        self.sink = _Sink()
        self.handler = KnowledgeCommandHandler(self.workflow, output=self.sink, actor="henry")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _hypothesis(self, claim: str = "Rising relative volume predicts outperformance."):
        self.handler.handle(f"/knowledge hypothesize {TOPIC} {claim}", {})
        return self.graph.records.list_by_topic(TOPIC, kind=MemoryKind.HYPOTHESIS)[0]

    def _recorded(self, content: str) -> str:
        record = self.graph.records.add(
            MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, source="broker", content=content)
        )
        return record.id

    def _file(self, hypothesis, direction: str, content: str) -> None:
        self.handler.handle(
            f"/knowledge evidence {hypothesis.id[:8]} {direction} {self._recorded(content)}", {}
        )

    def test_hypothesizing_records_an_idea_nothing_acts_on(self) -> None:
        record = self._hypothesis()

        self.assertEqual(record.kind, MemoryKind.HYPOTHESIS)
        self.assertEqual(record.status, MemoryStatus.PROPOSED)
        self.assertEqual(record.source, "user:henry", "provenance separates a person from a feed")
        self.assertIn("Nothing acts on it yet", self.sink.text)

    def test_the_reply_says_how_to_aim_evidence_at_it(self) -> None:
        record = self._hypothesis()
        self.assertIn(f"/knowledge evidence {record.id[:8]}", self.sink.text)

    def test_hypothesizing_needs_a_topic_and_a_claim(self) -> None:
        self.handler.handle("/knowledge hypothesize", {})
        self.handler.handle(f"/knowledge hypothesize {TOPIC}", {})

        self.assertEqual(self.graph.records.count(), 0)
        self.assertIn("Usage:", self.sink.errors)

    def test_a_flat_topic_earns_the_same_nudge(self) -> None:
        self.handler.handle("/knowledge hypothesize scratch an untested idea", {})

        self.assertIn("domain/subtopic", self.sink.text)
        self.assertEqual(len(self.graph.records.list_by_topic("scratch")), 1)

    def test_evidence_links_an_existing_record_and_re_reads_the_verdict(self) -> None:
        hypothesis = self._hypothesis()
        self.sink.lines.clear()

        self._file(hypothesis, "for", "ABC reached +3.4%")

        evidence = self.graph.evidence_for(hypothesis.id)
        self.assertEqual(len(evidence.supporting), 1)
        self.assertEqual(self.graph.records.get(hypothesis.id).status, MemoryStatus.TESTING)
        self.assertIn("1 of 5", self.sink.text)

    def test_the_link_records_who_filed_it(self) -> None:
        hypothesis = self._hypothesis()

        self._file(hypothesis, "for", "ABC reached +3.4%")

        _, link = self.graph.evidence_for(hypothesis.id).supporting[0]
        self.assertEqual(link.note, "user:henry")

    def test_a_note_is_kept_alongside_who_filed_it(self) -> None:
        hypothesis = self._hypothesis()
        outcome = self._recorded("ABC reached +3.4%")

        self.handler.handle(
            f"/knowledge evidence {hypothesis.id[:8]} for {outcome} thin volume though", {}
        )

        _, link = self.graph.evidence_for(hypothesis.id).supporting[0]
        self.assertEqual(link.note, "user:henry: thin volume though")

    def test_evidence_against_is_counted_against(self) -> None:
        hypothesis = self._hypothesis()

        self._file(hypothesis, "against", "ABC fell 2%")

        self.assertEqual(len(self.graph.evidence_for(hypothesis.id).contradicting), 1)

    def test_enough_supporting_evidence_reaches_the_approval_queue(self) -> None:
        hypothesis = self._hypothesis()
        for index in range(5):
            self._file(hypothesis, "for", f"win {index}")

        self.assertEqual(self.graph.records.get(hypothesis.id).status, MemoryStatus.SUPPORTED)
        self.assertIn("waiting for approval", self.sink.text)
        self.assertEqual([item.hypothesis.id for item in self.workflow.pending()], [hypothesis.id])

    def test_the_whole_loop_runs_from_the_command_line(self) -> None:
        """Observe, close, hypothesize, file the outcomes, approve, ask why."""
        self.handler.handle(f"/knowledge observe {TOPIC} ABC entered the candidate list at 10:04", {})
        observation = self.graph.records.list_by_topic(TOPIC, kind=MemoryKind.OBSERVATION)[0]
        self.handler.handle(f"/knowledge outcome {observation.id[:8]} ABC reached +3.4%", {})
        outcome = self.graph.outcome_for(observation.id)
        assert outcome is not None

        hypothesis = self._hypothesis()
        self.handler.handle(f"/knowledge evidence {hypothesis.id[:8]} for {outcome.id[:8]}", {})
        for index in range(4):
            self._file(hypothesis, "for", f"win {index}")
        self.handler.handle(f"/knowledge approve {hypothesis.id[:8]} ranking by relative volume", {})

        self.assertEqual(self.graph.records.get(hypothesis.id).status, MemoryStatus.ACCEPTED)
        self.sink.lines.clear()
        self.handler.handle(f"/knowledge why {hypothesis.id[:8]}", {})
        self.assertIn("ABC reached +3.4%", self.sink.text)
        self.assertIn(
            "ABC entered the candidate list", self.sink.text, "provenance reaches the observation"
        )

    def test_a_hypothesis_cannot_be_evidence_for_another(self) -> None:
        """Counting edges means a belief grounded in a belief would inflate itself."""
        first = self._hypothesis()
        self.handler.handle(f"/knowledge hypothesize {TOPIC} A second, unrelated idea.", {})
        others = [
            record
            for record in self.graph.records.list_by_topic(TOPIC, kind=MemoryKind.HYPOTHESIS)
            if record.id != first.id
        ]
        self.sink.lines.clear()

        self.handler.handle(f"/knowledge evidence {first.id[:8]} for {others[0].id[:8]}", {})

        self.assertIn("something that happened", self.sink.errors)
        self.assertEqual(self.graph.evidence_for(first.id).supporting, [])

    def test_evidence_never_reopens_what_a_person_settled(self) -> None:
        hypothesis = self._hypothesis()
        for index in range(5):
            self._file(hypothesis, "for", f"win {index}")
        self.handler.handle(f"/knowledge decline {hypothesis.id[:8]} too risky before the open", {})
        self.sink.lines.clear()

        self._file(hypothesis, "for", "another win")

        self.assertEqual(self.graph.records.get(hypothesis.id).status, MemoryStatus.REJECTED)
        self.assertIn("reopened by a person", self.sink.text)
        self.assertEqual(
            len(self.graph.evidence_for(hypothesis.id).supporting), 6, "the link is still written"
        )

    def test_contradicting_evidence_takes_it_back_out_of_the_queue(self) -> None:
        """Auto-promotion is forbidden; auto-demotion is the safe direction."""
        hypothesis = self._hypothesis()
        for index in range(5):
            self._file(hypothesis, "for", f"win {index}")
        self.assertEqual(len(self.workflow.pending()), 1)

        for index in range(5):
            self._file(hypothesis, "against", f"loss {index}")

        self.assertEqual(self.graph.records.get(hypothesis.id).status, MemoryStatus.TESTING)
        self.assertEqual(self.workflow.pending(), [])

    def test_evidence_needs_a_direction(self) -> None:
        hypothesis = self._hypothesis()
        outcome = self._recorded("ABC reached +3.4%")
        self.sink.lines.clear()

        self.handler.handle(f"/knowledge evidence {hypothesis.id[:8]} maybe {outcome}", {})

        self.assertIn("for|against", self.sink.errors)
        self.assertEqual(self.graph.evidence_for(hypothesis.id).supporting, [])

    def test_evidence_reports_an_unknown_id_rather_than_guessing(self) -> None:
        hypothesis = self._hypothesis()
        self.sink.lines.clear()

        self.handler.handle(f"/knowledge evidence {hypothesis.id[:8]} for zzzzzzzz", {})

        self.assertIn("Nothing matches", self.sink.errors)

    def test_evidence_attaches_to_a_hypothesis_not_an_observation(self) -> None:
        self.handler.handle(f"/knowledge observe {TOPIC} ABC entered the candidate list", {})
        observation = self.graph.records.list_by_topic(TOPIC, kind=MemoryKind.OBSERVATION)[0]
        outcome = self._recorded("ABC reached +3.4%")
        self.sink.lines.clear()

        self.handler.handle(f"/knowledge evidence {observation.id[:8]} for {outcome}", {})

        self.assertIn("not a observation", self.sink.errors)

    def test_testing_lists_what_is_still_gathering_evidence(self) -> None:
        hypothesis = self._hypothesis()
        self._file(hypothesis, "for", "ABC reached +3.4%")
        self.sink.lines.clear()

        self.handler.handle("/knowledge testing", {})

        self.assertIn("Rising relative volume", self.sink.text)
        self.assertIn("1 of 5", self.sink.text)

    def test_testing_drops_what_has_reached_a_verdict(self) -> None:
        hypothesis = self._hypothesis()
        for index in range(5):
            self._file(hypothesis, "for", f"win {index}")
        self.sink.lines.clear()

        self.handler.handle("/knowledge testing", {})

        self.assertIn("No hypotheses are under test", self.sink.text)

    def test_testing_can_be_narrowed_to_a_topic(self) -> None:
        self._hypothesis()
        self.handler.handle("/knowledge hypothesize iris/routing The planner over-chooses recall.", {})
        self.sink.lines.clear()

        self.handler.handle("/knowledge testing iris/routing", {})

        self.assertIn("over-chooses recall", self.sink.text)
        self.assertNotIn("Rising relative volume", self.sink.text)

    def test_testing_says_so_when_nothing_is_under_test(self) -> None:
        self.handler.handle("/knowledge testing", {})
        self.assertIn("No hypotheses are under test", self.sink.text)

    def test_the_usage_line_mentions_the_new_verbs(self) -> None:
        self.handler.handle("/knowledge", {})

        for verb in ("hypothesize", "evidence", "testing"):
            self.assertIn(verb, self.sink.text)
