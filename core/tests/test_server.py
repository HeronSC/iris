# File: core/tests/test_server.py

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.audit.logger import AuditLogger
from core.knowledge import KnowledgeGraph, KnowledgeRetriever, MemoryKind, MemoryRecord
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.review import KnowledgeReviewWorkflow
from core.server.app import create_app
from core.storage.sqlite_database import SQLiteDatabase

TOPIC = "trading/candidates"


class _Service:
    def __init__(self, root: Path) -> None:
        database = SQLiteDatabase(root / "knowledge.db")
        self.knowledge = KnowledgeGraph(database)
        self.knowledge_retriever = KnowledgeRetriever(database)
        self.knowledge_review = KnowledgeReviewWorkflow(
            HypothesisTracker(self.knowledge), audit_logger=AuditLogger(root / "audit")
        )
        self.config = {"assistant_name": "Iris"}


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = _Service(Path(self._tmp.name))
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _batch(self, count: int = 3, source: str = "bot:scanner") -> dict:
        return {
            "topic": TOPIC,
            "source": source,
            "observations": [
                {
                    "content": f"SYM{index} entered the candidate list, rel vol {1.0 + index / 10:.1f}x",
                    "data": {"symbol": f"SYM{index}", "rel_vol": 1.0 + index / 10},
                    "occurred_at": "2026-09-07T14:04:00+00:00",
                }
                for index in range(count)
            ],
        }

    def test_health_reports_what_is_stored_and_which_contract(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["records"], 0)
        self.assertEqual(response.json()["contract"], "shadow-1")

    def test_a_batch_of_candidates_is_accepted(self) -> None:
        response = self.client.post("/observations", json=self._batch(500))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["written"], 500)
        self.assertEqual(len(response.json()["ids"]), 500)
        self.assertEqual(self.service.knowledge.records.count(), 500)

    def test_the_feed_is_distinguishable_from_a_person(self) -> None:
        self.client.post("/observations", json=self._batch(1))

        stored = self.service.knowledge.records.list_by_topic(TOPIC)[0]
        self.assertEqual(stored.source, "bot:scanner")
        self.assertEqual(stored.kind, MemoryKind.OBSERVATION)
        self.assertEqual(stored.data["symbol"], "SYM0")
        self.assertEqual(stored.occurred_at, "2026-09-07T14:04:00+00:00")

    def test_a_batch_is_all_or_nothing_over_http(self) -> None:
        batch = self._batch(3)
        batch["observations"].append({"content": "  "})

        response = self.client.post("/observations", json=batch)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.service.knowledge.records.count(), 0)

    def test_an_empty_batch_is_refused(self) -> None:
        response = self.client.post("/observations", json={"topic": TOPIC, "source": "bot", "observations": []})

        self.assertEqual(response.status_code, 422)

    def test_topics_are_normalised_on_the_way_in(self) -> None:
        batch = self._batch(1)
        batch["topic"] = "Trading/Candidates"

        self.client.post("/observations", json=batch)

        self.assertEqual(len(self.service.knowledge.records.list_by_topic(TOPIC)), 1)

    def test_open_observations_are_listed(self) -> None:
        self.client.post("/observations", json=self._batch(3))

        response = self.client.get("/observations/open", params={"topic": TOPIC})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 3)

    def test_an_outcome_closes_one_and_it_leaves_the_open_list(self) -> None:
        ids = self.client.post("/observations", json=self._batch(3)).json()["ids"]

        response = self.client.post(
            f"/observations/{ids[0]}/outcome",
            json={"content": "SYM0 reached +3.4%", "source": "bot:broker"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["kind"], "outcome")
        remaining = self.client.get("/observations/open", params={"topic": TOPIC}).json()
        self.assertEqual(len(remaining), 2)

    def test_closing_something_that_is_not_there_is_a_404(self) -> None:
        response = self.client.post(
            "/observations/nope/outcome", json={"content": "x", "source": "bot"}
        )

        self.assertEqual(response.status_code, 404)

    def test_closing_a_record_that_is_not_an_observation_is_reported(self) -> None:
        hypothesis = self.service.knowledge.records.add(
            MemoryRecord(kind=MemoryKind.HYPOTHESIS, topic=TOPIC, source="analysis", content="An idea.")
        )

        response = self.client.post(
            f"/observations/{hypothesis.id}/outcome", json={"content": "x", "source": "bot"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("hypothesis", response.json()["detail"])

    def test_recall_finds_what_the_feed_wrote(self) -> None:
        self.client.post("/observations", json=self._batch(200))

        response = self.client.post("/recall", json={"text": "SYM7", "limit": 5})

        self.assertEqual(response.status_code, 200)
        records = response.json()["records"]
        self.assertTrue(records)
        self.assertIn("SYM7", records[0]["record"]["content"])

    def test_recall_reports_an_unknown_kind_rather_than_guessing(self) -> None:
        response = self.client.post("/recall", json={"text": "x", "kinds": ["nonsense"]})

        self.assertEqual(response.status_code, 422)

    def test_recall_carries_the_diagnostics_back(self) -> None:
        self.client.post("/observations", json=self._batch(5))

        response = self.client.post("/recall", json={"text": "candidate", "limit": 3})

        self.assertIn("diagnostics", response.json())

    def test_the_bot_feed_never_touches_conversation_state(self) -> None:
        """The service stub has no process_message; the endpoints must not need one."""
        self.assertFalse(hasattr(self.service, "process_message"))

        self.assertEqual(self.client.post("/observations", json=self._batch(2)).status_code, 201)
        self.assertEqual(self.client.post("/recall", json={"text": "SYM0"}).status_code, 200)


class SourceDecisionTests(unittest.TestCase):
    """The feed's own verdict comes in with the candidate, so the two can be compared later."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = _Service(Path(self._tmp.name))
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _with_decision(self, score: float = 0.42, content: str = "Rejected: below threshold") -> dict:
        return {
            "topic": TOPIC,
            "source": "bot:scanner",
            "observations": [
                {
                    "content": "ABC entered the candidate list, rel vol 2.1x",
                    "data": {"symbol": "ABC"},
                    "decision": {
                        "content": content,
                        "source": "bot:evaluator",
                        "score": score,
                        "data": {"regime": "bull", "pattern": "a", "threshold": 0.6},
                    },
                }
            ],
        }

    def test_the_feeds_verdict_is_stored_as_a_decision(self) -> None:
        response = self.client.post("/observations", json=self._with_decision())

        self.assertEqual(response.status_code, 201)
        decisions = self.service.knowledge.records.list_by_topic(TOPIC, kind=MemoryKind.DECISION)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].content, "Rejected: below threshold")
        self.assertEqual(decisions[0].source, "bot:evaluator")
        self.assertEqual(decisions[0].data["regime"], "bull")

    def test_the_feeds_own_score_is_recorded_not_trusted(self) -> None:
        """confidence is where a source's self-reported number lives; no verdict reads it."""
        self.client.post("/observations", json=self._with_decision(score=0.42))

        decision = self.service.knowledge.records.list_by_topic(TOPIC, kind=MemoryKind.DECISION)[0]
        self.assertEqual(decision.confidence, 0.42)

    def test_the_decision_points_back_at_what_it_was_decided_from(self) -> None:
        body = self.client.post("/observations", json=self._with_decision()).json()
        observation_id = body["ids"][0]
        decision_id = body["records"][0]["decision_id"]

        links = self.service.knowledge.links.links_from(decision_id)

        self.assertEqual([(link.target_id, link.relation.value) for link in links],
                         [(observation_id, "decided_from")])

    def test_provenance_reaches_the_decision_from_the_observation(self) -> None:
        body = self.client.post("/observations", json=self._with_decision()).json()
        decision_id = body["records"][0]["decision_id"]

        rendered = self.service.knowledge_review.explain(decision_id)

        self.assertIn("Rejected: below threshold", rendered)
        self.assertIn("ABC entered the candidate list", rendered)

    def test_a_candidate_without_a_decision_still_works(self) -> None:
        body = self.client.post(
            "/observations",
            json={"topic": TOPIC, "source": "bot", "observations": [{"content": "XYZ appeared"}]},
        ).json()

        self.assertIsNone(body["records"][0]["decision_id"])
        self.assertEqual(self.service.knowledge.records.count(), 1)

    def test_a_score_outside_zero_to_one_is_refused(self) -> None:
        batch = self._with_decision(score=1.4)

        response = self.client.post("/observations", json=batch)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.service.knowledge.records.count(), 0)


class BatchOutcomeTests(unittest.TestCase):
    """End of day closes every candidate, including the ones never traded."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = _Service(Path(self._tmp.name))
        self.client = TestClient(create_app(self.service))
        self.ids = self.client.post(
            "/observations",
            json={
                "topic": TOPIC,
                "source": "bot:scanner",
                "observations": [
                    {"content": f"SYM{i} entered the candidate list"} for i in range(50)
                ],
            },
        ).json()["ids"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _closing(self, count: int) -> dict:
        return {
            "source": "bot:broker",
            "outcomes": [
                {
                    "observation_id": self.ids[i],
                    "content": f"SYM{i} finished {'up' if i % 2 else 'down'}",
                    "favourable": bool(i % 2),
                }
                for i in range(count)
            ],
        }

    def test_a_days_candidates_close_in_one_call(self) -> None:
        response = self.client.post("/outcomes", json=self._closing(50))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["written"], 50)
        self.assertEqual(self.client.get("/observations/open", params={"topic": TOPIC}).json(), [])

    def test_the_favourable_flag_is_kept_where_counting_can_find_it(self) -> None:
        self.client.post("/outcomes", json=self._closing(2))

        outcomes = self.service.knowledge.outcomes_for(self.ids[:2])

        self.assertEqual(outcomes[self.ids[0]].data["favourable"], False)
        self.assertEqual(outcomes[self.ids[1]].data["favourable"], True)

    def test_an_outcome_inherits_the_topic_of_what_it_closes(self) -> None:
        self.client.post("/outcomes", json=self._closing(1))

        self.assertEqual(self.service.knowledge.outcomes_for(self.ids[:1])[self.ids[0]].topic, TOPIC)

    def test_one_unknown_id_fails_the_whole_batch(self) -> None:
        batch = self._closing(3)
        batch["outcomes"].append({"observation_id": "nope", "content": "x"})

        response = self.client.post("/outcomes", json=batch)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.client.get("/observations/open", params={"topic": TOPIC}).json()), 50)

    def test_an_unlabelled_outcome_is_allowed_but_carries_no_flag(self) -> None:
        self.client.post(
            "/outcomes",
            json={"source": "bot", "outcomes": [{"observation_id": self.ids[0], "content": "closed"}]},
        )

        self.assertNotIn("favourable", self.service.knowledge.outcomes_for(self.ids[:1])[self.ids[0]].data)


class AssessmentContractTests(unittest.TestCase):
    """The contract is live and inert: it says what it knows, which at first is nothing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = _Service(Path(self._tmp.name))
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _post(self, contents: list[str], topic: str = TOPIC) -> list[str]:
        return self.client.post(
            "/observations",
            json={
                "topic": topic,
                "source": "bot:scanner",
                "observations": [{"content": text} for text in contents],
            },
        ).json()["ids"]

    def _close(self, pairs: list[tuple[str, bool]]) -> None:
        self.client.post(
            "/outcomes",
            json={
                "source": "bot:broker",
                "outcomes": [
                    {"observation_id": item, "content": "closed", "favourable": good}
                    for item, good in pairs
                ],
            },
        )

    def test_an_empty_store_says_it_has_no_basis(self) -> None:
        ids = self._post(["ABC entered the candidate list, rel vol 2.1x"])

        body = self.client.post("/assess", json={"ids": ids}).json()

        self.assertEqual(body["contract"], "shadow-1")
        self.assertFalse(body["binding"])
        self.assertEqual(body["ranked"], 0)
        self.assertEqual(body["appraisals"][0]["assessment"]["basis"], "none")
        self.assertIsNone(body["appraisals"][0]["assessment"]["score"])

    def test_nothing_is_ranked_when_nothing_can_be_scored(self) -> None:
        """A rank on no basis is a false signal; abstention includes not ordering."""
        ids = self._post([f"SYM{i} entered the candidate list" for i in range(20)])

        body = self.client.post("/assess", json={"ids": ids}).json()

        self.assertTrue(all(item["rank"] is None for item in body["appraisals"]))

    def test_every_appraisal_is_marked_not_binding(self) -> None:
        ids = self._post(["ABC entered the candidate list"])

        body = self.client.post("/assess", json={"ids": ids}).json()

        self.assertTrue(all(item["binding"] is False for item in body["appraisals"]))
        self.assertTrue(all(item["contract"] == "shadow-1" for item in body["appraisals"]))

    def test_similar_but_unresolved_observations_are_still_no_basis(self) -> None:
        self._post([f"widget alpha spike number {i}" for i in range(10)], topic="lab/widgets")
        ids = self._post(["widget alpha spike number 99"], topic="lab/widgets")

        body = self.client.post("/assess", json={"ids": ids}).json()

        assessment = body["appraisals"][0]["assessment"]
        self.assertEqual(assessment["basis"], "none")
        self.assertIn("none of them closed", assessment["rationale"])

    def test_too_few_outcomes_reports_the_count_but_withholds_a_rate(self) -> None:
        earlier = self._post([f"widget beta spike number {i}" for i in range(3)], topic="lab/widgets")
        self._close([(earlier[0], True), (earlier[1], True), (earlier[2], False)])
        ids = self._post(["widget beta spike number 99"], topic="lab/widgets")

        assessment = self.client.post("/assess", json={"ids": ids}).json()["appraisals"][0]["assessment"]

        self.assertEqual(assessment["basis"], "observations")
        self.assertEqual(assessment["sample"], 3)
        self.assertIsNone(assessment["score"], "a rate on three points is not a rate")
        self.assertIn("3 of 5", assessment["rationale"])

    def test_enough_outcomes_produce_an_observed_rate(self) -> None:
        earlier = self._post([f"widget gamma spike number {i}" for i in range(6)], topic="lab/widgets")
        self._close([(item, index < 4) for index, item in enumerate(earlier)])
        ids = self._post(["widget gamma spike number 99"], topic="lab/widgets")

        body = self.client.post("/assess", json={"ids": ids}).json()
        assessment = body["appraisals"][0]["assessment"]

        self.assertEqual(assessment["basis"], "observations")
        self.assertEqual(assessment["sample"], 6)
        self.assertEqual(assessment["favourable"], 4)
        self.assertEqual(assessment["score"], 0.6667)
        self.assertEqual(body["ranked"], 1)
        self.assertEqual(body["appraisals"][0]["rank"], 1)
        self.assertIn("not a prediction", assessment["rationale"])

    def test_the_score_never_comes_from_a_reported_confidence(self) -> None:
        """A source can claim 1.0 all day; the rate is counted from outcomes only."""
        self.client.post(
            "/observations",
            json={
                "topic": "lab/widgets",
                "source": "bot",
                "observations": [
                    {
                        "content": f"widget delta spike number {i}",
                        "decision": {"content": "Bought, certain", "score": 1.0},
                    }
                    for i in range(6)
                ],
            },
        )
        earlier = [
            item.id
            for item in self.service.knowledge.records.list_by_topic(
                "lab/widgets", kind=MemoryKind.OBSERVATION, limit=10
            )
        ]
        self._close([(item, False) for item in earlier])
        ids = self._post(["widget delta spike number 99"], topic="lab/widgets")

        assessment = self.client.post("/assess", json={"ids": ids}).json()["appraisals"][0]["assessment"]

        self.assertEqual(assessment["score"], 0.0, "six confident buys that all went badly score zero")

    def test_an_unknown_id_is_reported(self) -> None:
        response = self.client.post("/assess", json={"ids": ["no-such-record"]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("No such memory", response.json()["detail"])

    def test_assessing_nothing_is_refused(self) -> None:
        self.assertEqual(self.client.post("/assess", json={"ids": []}).status_code, 422)


class HitRateComparisonTests(unittest.TestCase):
    """Hit rate at top N, between whoever scored the same morning."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = _Service(Path(self._tmp.name))
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _morning(self, cohort: str, rows: list[tuple[float, bool]]) -> list[str]:
        ids = self.client.post(
            "/observations",
            json={
                "topic": TOPIC,
                "source": "bot:scanner",
                "source_ref": cohort,
                "observations": [
                    {
                        "content": f"SYM{index} entered the candidate list",
                        "decision": {
                            "content": f"bot confidence {own}",
                            "source": "bot:evaluator",
                            "score": own,
                        },
                    }
                    for index, (own, _) in enumerate(rows)
                ],
            },
        ).json()["ids"]
        self.client.post(
            "/outcomes",
            json={
                "source": "bot:broker",
                "outcomes": [
                    {"observation_id": item, "content": "closed", "favourable": good}
                    for item, (_, good) in zip(ids, rows)
                ],
            },
        )
        return ids

    def _perfect_and_useless(self) -> list[str]:
        rows = [(0.9, True)] * 10 + [(0.1, False)] * 10
        return self._morning("2026-09-07", rows)

    def test_a_cohort_is_gathered_by_source_ref(self) -> None:
        self._perfect_and_useless()
        self._morning("2026-09-08", [(0.5, True)] * 4)

        body = self.client.post("/compare", json={"cohort": "2026-09-07", "top_n": 10}).json()

        self.assertEqual(body["size"], 20)
        self.assertEqual(body["resolved"], 20)

    def test_a_ranker_that_sorts_perfectly_scores_one(self) -> None:
        self._perfect_and_useless()

        body = self.client.post("/compare", json={"cohort": "2026-09-07", "top_n": 10}).json()

        bot = [item for item in body["rankers"] if item["source"] == "bot:evaluator"][0]
        self.assertEqual(bot["hits"], 10)
        self.assertEqual(bot["hit_rate"], 1.0)
        self.assertEqual(body["base_rate"], 0.5)
        self.assertEqual(bot["lift"], 0.5)
        self.assertEqual(body["leader"], "bot:evaluator")

    def test_a_ranker_that_sorts_backwards_scores_zero(self) -> None:
        self._morning("bad", [(0.1, True)] * 10 + [(0.9, False)] * 10)

        body = self.client.post("/compare", json={"cohort": "bad", "top_n": 10}).json()

        bot = [item for item in body["rankers"] if item["source"] == "bot:evaluator"][0]
        self.assertEqual(bot["hit_rate"], 0.0)
        self.assertEqual(bot["lift"], -0.5)

    def test_lift_is_what_separates_a_ranker_from_luck(self) -> None:
        """Everything favourable means any top N scores 1.0 and deserves no credit."""
        self._morning("easy", [(0.9, True)] * 6 + [(0.1, True)] * 6)

        body = self.client.post("/compare", json={"cohort": "easy", "top_n": 6}).json()

        bot = [item for item in body["rankers"] if item["source"] == "bot:evaluator"][0]
        self.assertEqual(bot["hit_rate"], 1.0)
        self.assertEqual(bot["lift"], 0.0, "beating a 100% base rate is not skill")

    def test_iris_is_compared_only_on_what_it_recorded_at_the_time(self) -> None:
        """Recording is what stops the comparison grading Iris with the answers."""
        ids = self._perfect_and_useless()
        self.client.post("/assess", json={"ids": ids, "record": True})

        body = self.client.post("/compare", json={"cohort": "2026-09-07", "top_n": 10}).json()

        sources = {item["source"] for item in body["rankers"]}
        self.assertIn("iris:shadow-1", sources)
        self.assertIn("bot:evaluator", sources)

    def test_an_unrecorded_assessment_takes_no_part(self) -> None:
        ids = self._perfect_and_useless()
        self.client.post("/assess", json={"ids": ids})

        body = self.client.post("/compare", json={"cohort": "2026-09-07", "top_n": 10}).json()

        self.assertEqual([item["source"] for item in body["rankers"]], ["bot:evaluator"])

    def test_recording_reports_how_many_it_kept(self) -> None:
        ids = self._perfect_and_useless()

        body = self.client.post("/assess", json={"ids": ids, "record": True}).json()

        self.assertEqual(body["recorded"], 20)
        self.assertEqual(
            len(self.service.knowledge.records.list_by_topic(TOPIC, kind=MemoryKind.DECISION, limit=100)),
            40,
            "twenty from the bot, twenty from Iris",
        )

    def test_a_recorded_appraisal_points_back_at_its_candidate(self) -> None:
        ids = self._perfect_and_useless()
        self.client.post("/assess", json={"ids": ids, "record": True})

        decisions = self.service.knowledge.decisions_for([ids[0]])[ids[0]]

        sources = {item.source for item in decisions}
        self.assertEqual(sources, {"bot:evaluator", "iris:shadow-1"})

    def test_ties_at_the_cut_are_reported_not_hidden(self) -> None:
        self._morning("flat", [(0.5, index % 2 == 0) for index in range(20)])

        body = self.client.post("/compare", json={"cohort": "flat", "top_n": 5}).json()

        bot = [item for item in body["rankers"] if item["source"] == "bot:evaluator"][0]
        self.assertEqual(bot["tied_at_the_cut"], 20)
        self.assertTrue(any("arbitrary" in note for note in body["notes"]))

    def test_a_thin_cohort_says_so_rather_than_pretending(self) -> None:
        self._morning("thin", [(0.9, True), (0.1, False)])

        body = self.client.post("/compare", json={"cohort": "thin", "top_n": 50}).json()

        self.assertTrue(any("noise" in note for note in body["notes"]))
        self.assertTrue(any("only 2" in note for note in body["notes"]))

    def test_unresolved_candidates_are_excluded_and_counted(self) -> None:
        self.client.post(
            "/observations",
            json={
                "topic": TOPIC,
                "source": "bot:scanner",
                "source_ref": "partial",
                "observations": [{"content": f"SYM{i} appeared"} for i in range(10)],
            },
        )

        body = self.client.post("/compare", json={"cohort": "partial", "top_n": 5}).json()

        self.assertEqual(body["size"], 10)
        self.assertEqual(body["resolved"], 0)
        self.assertIsNone(body["base_rate"])
        self.assertIn("Nothing is resolved yet", body["summary"])

    def test_an_unknown_cohort_is_reported(self) -> None:
        response = self.client.post("/compare", json={"cohort": "never-happened", "top_n": 5})

        self.assertEqual(response.status_code, 400)
        self.assertIn("No observations", response.json()["detail"])

    def test_the_summary_reads_as_a_sentence(self) -> None:
        self._perfect_and_useless()

        summary = self.client.post("/compare", json={"cohort": "2026-09-07", "top_n": 10}).json()["summary"]

        self.assertIn("20 candidates", summary)
        self.assertIn("Base rate 50.0%", summary)
        self.assertIn("bot:evaluator", summary)
