from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.knowledge import (
    KnowledgeError,
    KnowledgeGraph,
    MemoryKind,
    MemoryRecord,
    MemoryRelation,
    MemoryStatus,
)
from core.knowledge.hypotheses import Assessment, HypothesisPolicy, HypothesisTracker
from core.storage.sqlite_database import SQLiteDatabase

TOPIC = "trading/candidates"


class HypothesisTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))
        self.tracker = HypothesisTracker(self.graph)
        self.hypothesis = self.tracker.propose(
            "Rising relative volume in the first 45 minutes predicts outperformance.",
            topic=TOPIC,
            source="analysis",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _evidence(self, supporting: int, contradicting: int) -> None:
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

    # -- the lifecycle -----------------------------------------------------

    def test_a_new_hypothesis_starts_unproven(self) -> None:
        self.assertEqual(self.hypothesis.status, MemoryStatus.PROPOSED)
        self.assertEqual(self.hypothesis.kind, MemoryKind.HYPOTHESIS)

    def test_testing_begins_explicitly(self) -> None:
        moved = self.tracker.begin_testing(self.hypothesis.id)
        self.assertEqual(moved.status, MemoryStatus.TESTING)

    def test_beginning_testing_twice_is_harmless(self) -> None:
        self.tracker.begin_testing(self.hypothesis.id)
        self.assertEqual(self.tracker.begin_testing(self.hypothesis.id).status, MemoryStatus.TESTING)

    def test_a_settled_hypothesis_cannot_re_enter_testing(self) -> None:
        self._evidence(supporting=5, contradicting=0)
        self.tracker.evaluate(self.hypothesis.id)

        with self.assertRaises(KnowledgeError):
            self.tracker.begin_testing(self.hypothesis.id)

    def test_evidence_carries_it_into_testing(self) -> None:
        self._evidence(supporting=1, contradicting=0)

        assessment = self.tracker.evaluate(self.hypothesis.id)

        self.assertEqual(assessment.recommended, MemoryStatus.TESTING)
        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.TESTING)

    def test_a_verdict_waits_for_enough_evidence(self) -> None:
        """Four agreeing data points should not settle anything."""
        self._evidence(supporting=4, contradicting=0)

        assessment = self.tracker.evaluate(self.hypothesis.id)

        self.assertEqual(assessment.recommended, MemoryStatus.TESTING)
        self.assertIn("4 of 5", assessment.rationale)

    def test_enough_agreement_reaches_supported(self) -> None:
        self._evidence(supporting=5, contradicting=0)

        assessment = self.tracker.evaluate(self.hypothesis.id)

        self.assertEqual(assessment.recommended, MemoryStatus.SUPPORTED)
        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.SUPPORTED)
        self.assertIn("requires approval", assessment.rationale)

    def test_enough_disagreement_reaches_rejected(self) -> None:
        self._evidence(supporting=1, contradicting=6)

        assessment = self.tracker.evaluate(self.hypothesis.id)

        self.assertEqual(assessment.recommended, MemoryStatus.REJECTED)
        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.REJECTED)

    def test_a_split_verdict_stays_under_test(self) -> None:
        self._evidence(supporting=3, contradicting=3)

        assessment = self.tracker.evaluate(self.hypothesis.id)

        self.assertEqual(assessment.recommended, MemoryStatus.TESTING)
        self.assertIn("Inconclusive", assessment.rationale)

    # -- the line the design draws ------------------------------------------

    def test_evidence_alone_never_reaches_accepted(self) -> None:
        """However one-sided the evidence, adoption is not automatic."""
        self._evidence(supporting=50, contradicting=0)

        for _ in range(3):
            assessment = self.tracker.evaluate(self.hypothesis.id)

        self.assertEqual(assessment.recommended, MemoryStatus.SUPPORTED)
        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.SUPPORTED)

    def test_promotion_records_who_approved_it(self) -> None:
        self._evidence(supporting=5, contradicting=0)
        self.tracker.evaluate(self.hypothesis.id)

        promoted = self.tracker.promote(self.hypothesis.id, approved_by="henry")

        self.assertEqual(promoted.status, MemoryStatus.ACCEPTED)
        decisions = self.graph.records.list_by_topic(TOPIC, kind=MemoryKind.DECISION)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].data["approved_by"], "henry")
        self.assertIn("approval:henry", decisions[0].source)

    def test_the_approval_is_part_of_the_audit_trail(self) -> None:
        self._evidence(supporting=5, contradicting=0)
        self.tracker.evaluate(self.hypothesis.id)
        self.tracker.promote(self.hypothesis.id, approved_by="henry")

        decision = self.graph.records.list_by_topic(TOPIC, kind=MemoryKind.DECISION)[0]
        explanation = self.graph.explain(decision.id)

        self.assertIn(self.hypothesis.id, [item.id for item in explanation.flatten()])
        self.assertEqual(explanation.grounds[0].relation, MemoryRelation.DECIDED_FROM)

    def test_promotion_requires_a_name(self) -> None:
        self._evidence(supporting=5, contradicting=0)
        self.tracker.evaluate(self.hypothesis.id)

        with self.assertRaises(KnowledgeError):
            self.tracker.promote(self.hypothesis.id, approved_by="   ")

    def test_an_unsupported_hypothesis_cannot_be_promoted(self) -> None:
        with self.assertRaises(KnowledgeError) as caught:
            self.tracker.promote(self.hypothesis.id, approved_by="henry")

        self.assertIn("Only a supported hypothesis", str(caught.exception))

    def test_an_accepted_hypothesis_is_not_reopened_by_later_evidence(self) -> None:
        self._evidence(supporting=5, contradicting=0)
        self.tracker.evaluate(self.hypothesis.id)
        self.tracker.promote(self.hypothesis.id, approved_by="henry")

        self._evidence(supporting=0, contradicting=20)
        assessment = self.tracker.evaluate(self.hypothesis.id)

        self.assertEqual(assessment.recommended, MemoryStatus.ACCEPTED)
        self.assertEqual(self.graph.records.get(self.hypothesis.id).status, MemoryStatus.ACCEPTED)
        self.assertIn("does not reopen", assessment.rationale)

    # -- reading without writing ---------------------------------------------

    def test_assess_changes_nothing(self) -> None:
        self._evidence(supporting=5, contradicting=0)

        assessment = self.tracker.assess(self.hypothesis.id)

        self.assertEqual(assessment.recommended, MemoryStatus.SUPPORTED)
        self.assertTrue(assessment.would_change)
        self.assertEqual(
            self.graph.records.get(self.hypothesis.id).status,
            MemoryStatus.PROPOSED,
            "assess must be a preview, not an action",
        )

    def test_an_assessment_counts_both_sides(self) -> None:
        self._evidence(supporting=4, contradicting=2)

        assessment = self.tracker.assess(self.hypothesis.id)

        self.assertEqual((assessment.supporting, assessment.contradicting, assessment.total), (4, 2, 6))

    def test_confidence_plays_no_part_in_the_verdict(self) -> None:
        """A fluent guess must not be able to promote itself."""
        confident = self.tracker.propose(
            "A very confident claim.", topic=TOPIC, source="llm", confidence=1.0
        )
        self.graph.add_evidence(
            confident.id,
            MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, source="broker", content="one result",
                         confidence=1.0),
            supports=True,
        )

        assessment = self.tracker.evaluate(confident.id)

        self.assertEqual(assessment.recommended, MemoryStatus.TESTING)
        self.assertNotIn("confidence", assessment.rationale.lower())

    # -- finding work ---------------------------------------------------------

    def test_awaiting_approval_lists_what_needs_a_person(self) -> None:
        self._evidence(supporting=5, contradicting=0)
        self.tracker.evaluate(self.hypothesis.id)
        untested = self.tracker.propose("Another idea.", topic=TOPIC, source="analysis")

        waiting = self.tracker.awaiting_approval()

        self.assertEqual([item.id for item in waiting], [self.hypothesis.id])
        self.assertNotIn(untested.id, [item.id for item in waiting])

    def test_work_can_be_narrowed_to_a_topic(self) -> None:
        elsewhere = self.tracker.propose("Unrelated idea.", topic="home/network", source="analysis")

        self.assertEqual(
            [item.id for item in self.tracker.in_status(MemoryStatus.PROPOSED, topic="home/network")],
            [elsewhere.id],
        )

    # -- guards ----------------------------------------------------------------

    def test_a_missing_hypothesis_is_refused(self) -> None:
        with self.assertRaises(KnowledgeError):
            self.tracker.assess("ghost")

    def test_only_a_hypothesis_can_be_tracked(self) -> None:
        observation = self.graph.records.add(
            MemoryRecord(kind=MemoryKind.OBSERVATION, topic=TOPIC, source="scanner", content="ABC at 10:04")
        )

        with self.assertRaises(KnowledgeError) as caught:
            self.tracker.assess(observation.id)

        self.assertIn("Not a hypothesis", str(caught.exception))


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_stricter_policy_demands_more_evidence(self) -> None:
        tracker = HypothesisTracker(self.graph, HypothesisPolicy(minimum_evidence=20, support_ratio=0.9))
        hypothesis = tracker.propose("An idea.", topic=TOPIC, source="analysis")
        for index in range(10):
            self.graph.add_evidence(
                hypothesis.id,
                MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, source="broker", content=f"win {index}"),
                supports=True,
            )

        self.assertEqual(tracker.evaluate(hypothesis.id).recommended, MemoryStatus.TESTING)

    def test_an_incoherent_policy_is_refused(self) -> None:
        for bad in (
            {"minimum_evidence": 0},
            {"support_ratio": 0.4},
            {"rejection_ratio": 1.5},
        ):
            with self.assertRaises(KnowledgeError):
                HypothesisPolicy(**bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
