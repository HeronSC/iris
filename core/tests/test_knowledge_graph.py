from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.knowledge import (
    KnowledgeError,
    KnowledgeGraph,
    MemoryKind,
    MemoryLink,
    MemoryRecord,
    MemoryRelation,
    render_explanation,
)
from core.storage.sqlite_database import SQLiteDatabase


def _record(kind: MemoryKind, content: str, **overrides: object) -> MemoryRecord:
    values: dict[str, object] = {
        "kind": kind,
        "topic": "trading/candidates",
        "content": content,
        "source": "scanner",
    }
    values.update(overrides)
    return MemoryRecord(**values)  # type: ignore[arg-type]


class LinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))
        self.records = self.graph.records
        self.links = self.graph.links

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_link_needs_both_ends_to_exist(self) -> None:
        """Foreign keys are on, and the failure should read as a domain error."""
        real = self.records.add(_record(MemoryKind.OBSERVATION, "ABC at 10:04"))

        with self.assertRaises(KnowledgeError) as caught:
            self.links.link(real.id, "ghost", MemoryRelation.RELATES_TO)

        self.assertIn("existing memories", str(caught.exception))

    def test_the_same_edge_cannot_be_recorded_twice(self) -> None:
        first = self.records.add(_record(MemoryKind.OBSERVATION, "a"))
        second = self.records.add(_record(MemoryKind.OBSERVATION, "b"))
        self.links.link(first.id, second.id, MemoryRelation.RELATES_TO)

        with self.assertRaises(KnowledgeError) as caught:
            self.links.link(first.id, second.id, MemoryRelation.RELATES_TO)

        self.assertIn("already exists", str(caught.exception))

    def test_the_same_pair_can_hold_different_relations(self) -> None:
        first = self.records.add(_record(MemoryKind.OBSERVATION, "a"))
        second = self.records.add(_record(MemoryKind.OBSERVATION, "b"))

        self.links.link(first.id, second.id, MemoryRelation.RELATES_TO)
        self.links.link(first.id, second.id, MemoryRelation.DERIVED_FROM)

        self.assertEqual(len(self.links.links_from(first.id)), 2)

    def test_a_memory_cannot_link_to_itself(self) -> None:
        with self.assertRaises(KnowledgeError):
            MemoryLink(source_id="x", target_id="x", relation=MemoryRelation.RELATES_TO)


class OutcomeLinkageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_observation_can_be_closed_by_an_outcome_later(self) -> None:
        observation = self.graph.records.add(
            _record(MemoryKind.OBSERVATION, "ABC entered the candidate list at 10:04.")
        )
        self.assertIsNone(self.graph.outcome_for(observation.id))

        self.graph.record_outcome(
            observation.id, _record(MemoryKind.OUTCOME, "ABC reached +3.4%.", source="broker")
        )

        outcome = self.graph.outcome_for(observation.id)
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.content, "ABC reached +3.4%.")
        self.assertEqual(outcome.source, "broker")

    def test_recording_an_outcome_against_a_missing_observation_is_refused(self) -> None:
        with self.assertRaises(KnowledgeError):
            self.graph.record_outcome("ghost", _record(MemoryKind.OUTCOME, "n/a"))

    def test_only_an_outcome_can_close_an_observation(self) -> None:
        observation = self.graph.records.add(_record(MemoryKind.OBSERVATION, "ABC at 10:04"))

        with self.assertRaises(KnowledgeError) as caught:
            self.graph.record_outcome(observation.id, _record(MemoryKind.DECISION, "bought ABC"))

        self.assertIn("Expected an outcome", str(caught.exception))

    def test_open_observations_are_the_ones_still_waiting(self) -> None:
        closed = self.graph.records.add(_record(MemoryKind.OBSERVATION, "closed one"))
        still_open = self.graph.records.add(_record(MemoryKind.OBSERVATION, "open one"))
        self.graph.record_outcome(closed.id, _record(MemoryKind.OUTCOME, "+3.4%"))

        open_ones = self.graph.open_observations("trading/candidates")

        self.assertEqual([item.content for item in open_ones], ["open one"])
        self.assertNotIn(closed.id, [item.id for item in open_ones])
        self.assertIn(still_open.id, [item.id for item in open_ones])


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))
        self.hypothesis = self.graph.records.add(
            _record(
                MemoryKind.HYPOTHESIS,
                "Rising relative volume in the first 45 minutes predicts outperformance.",
                source="analysis",
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_evidence_accumulates_on_both_sides(self) -> None:
        self.graph.add_evidence(
            self.hypothesis.id, _record(MemoryKind.OUTCOME, "ABC +3.4%"), supports=True, weight=0.8
        )
        self.graph.add_evidence(
            self.hypothesis.id, _record(MemoryKind.OUTCOME, "DEF +2.9%"), supports=True
        )
        self.graph.add_evidence(
            self.hypothesis.id, _record(MemoryKind.OUTCOME, "GHI -1.2%"), supports=False,
            note="high volume, went nowhere",
        )

        evidence = self.graph.evidence_for(self.hypothesis.id)

        self.assertEqual(len(evidence.supporting), 2)
        self.assertEqual(len(evidence.contradicting), 1)
        self.assertEqual(evidence.balance, 1)
        self.assertEqual(evidence.supporting[0][1].weight, 0.8)
        self.assertEqual(evidence.contradicting[0][1].note, "high volume, went nowhere")

    def test_evidence_only_attaches_to_a_hypothesis(self) -> None:
        observation = self.graph.records.add(_record(MemoryKind.OBSERVATION, "ABC at 10:04"))

        with self.assertRaises(KnowledgeError) as caught:
            self.graph.add_evidence(observation.id, _record(MemoryKind.OUTCOME, "x"), supports=True)

        self.assertIn("attaches to a hypothesis", str(caught.exception))

    def test_an_existing_record_can_be_cited_as_evidence(self) -> None:
        """The same outcome may bear on more than one hypothesis."""
        outcome = self.graph.records.add(_record(MemoryKind.OUTCOME, "ABC +3.4%"))
        other = self.graph.records.add(
            _record(MemoryKind.HYPOTHESIS, "Morning gaps fill by noon.", source="analysis")
        )

        self.graph.add_evidence(self.hypothesis.id, outcome, supports=True)
        self.graph.add_evidence(other.id, outcome, supports=False)

        self.assertEqual(self.graph.records.count(), 3, "citing must not duplicate the record")
        self.assertEqual(self.graph.evidence_for(self.hypothesis.id).balance, 1)
        self.assertEqual(self.graph.evidence_for(other.id).balance, -1)

    def test_a_hypothesis_with_no_evidence_is_balanced_at_zero(self) -> None:
        evidence = self.graph.evidence_for(self.hypothesis.id)
        self.assertEqual(evidence.balance, 0)
        self.assertEqual(evidence.supporting, [])


class ExplanationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_belief_traces_back_to_what_was_observed(self) -> None:
        """The question the phase exists to answer: why does Iris believe this?"""
        observation = self.graph.records.add(
            _record(MemoryKind.OBSERVATION, "ABC showed 2.1x relative volume at 10:04.")
        )
        outcome = self.graph.record_outcome(
            observation.id, _record(MemoryKind.OUTCOME, "ABC reached +3.4%.")
        )
        hypothesis = self.graph.records.add(
            _record(MemoryKind.HYPOTHESIS, "Relative volume predicts outperformance.", source="analysis")
        )
        self.graph.add_evidence(hypothesis.id, outcome, supports=True)
        rule = self.graph.records.add(
            _record(MemoryKind.KNOWLEDGE, "Rank candidates by relative volume.", source="analysis")
        )
        self.graph.links.link(rule.id, hypothesis.id, MemoryRelation.DERIVED_FROM)

        explanation = self.graph.explain(rule.id)
        contents = [item.content for item in explanation.flatten()]

        self.assertEqual(explanation.record.id, rule.id)
        self.assertIn("Relative volume predicts outperformance.", contents)
        self.assertIn("ABC reached +3.4%.", contents)
        self.assertFalse(explanation.truncated)

    def test_association_alone_is_not_treated_as_grounds(self) -> None:
        rule = self.graph.records.add(_record(MemoryKind.KNOWLEDGE, "a rule", source="analysis"))
        aside = self.graph.records.add(_record(MemoryKind.OBSERVATION, "an unrelated aside"))
        self.graph.links.link(rule.id, aside.id, MemoryRelation.RELATES_TO)

        explanation = self.graph.explain(rule.id)

        self.assertEqual(explanation.grounds, [])
        self.assertFalse(explanation.truncated)

    def test_depth_is_bounded_and_the_cut_is_visible(self) -> None:
        chain = [self.graph.records.add(_record(MemoryKind.KNOWLEDGE, f"step {i}", source="a")) for i in range(5)]
        for upper, lower in zip(chain, chain[1:]):
            self.graph.links.link(upper.id, lower.id, MemoryRelation.DERIVED_FROM)

        shallow = self.graph.explain(chain[0].id, max_depth=2)

        self.assertEqual([item.content for item in shallow.flatten()], ["step 0", "step 1", "step 2"])
        deepest = shallow.grounds[0].grounds[0]
        self.assertTrue(deepest.truncated, "a cut-off branch must not look like a leaf")

    def test_a_cycle_does_not_hang_the_walk(self) -> None:
        first = self.graph.records.add(_record(MemoryKind.KNOWLEDGE, "first", source="a"))
        second = self.graph.records.add(_record(MemoryKind.KNOWLEDGE, "second", source="a"))
        self.graph.links.link(first.id, second.id, MemoryRelation.DERIVED_FROM)
        self.graph.links.link(second.id, first.id, MemoryRelation.DERIVED_FROM)

        explanation = self.graph.explain(first.id)

        self.assertEqual([item.content for item in explanation.flatten()], ["first", "second"])
        self.assertTrue(explanation.grounds[0].truncated)

    def test_explaining_a_missing_memory_is_refused(self) -> None:
        with self.assertRaises(KnowledgeError):
            self.graph.explain("ghost")

    def test_rendering_reads_as_an_answer(self) -> None:
        hypothesis = self.graph.records.add(
            _record(MemoryKind.HYPOTHESIS, "Volume predicts outperformance.", source="analysis")
        )
        self.graph.add_evidence(hypothesis.id, _record(MemoryKind.OUTCOME, "ABC reached +3.4%."), supports=True)

        text = render_explanation(self.graph.explain(hypothesis.id))

        self.assertEqual(
            text,
            "Volume predicts outperformance.\n  - ABC reached +3.4%. (supported by)",
        )


if __name__ == "__main__":
    unittest.main()
