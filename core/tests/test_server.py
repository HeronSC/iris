# File: core/tests/test_server.py

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.audit.logger import AuditLogger
from core.knowledge import KnowledgeGraph, KnowledgeRetriever, MemoryKind, MemoryRecord, MemoryStatus
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
        self.assertEqual(response.json()["contract"], "shadow-2")

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

        self.assertEqual(body["contract"], "shadow-2")
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
        self.assertTrue(all(item["contract"] == "shadow-2" for item in body["appraisals"]))

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

    def _cohort(self, cohort: str, rows: list[tuple[float, bool | None]], topic: str = TOPIC) -> list[str]:
        ids = self.client.post(
            "/observations",
            json={
                "topic": topic,
                "source": "bot:scanner",
                "source_ref": cohort,
                "observations": [
                    {"content": f"widget spike at {level}", "data": {"level": level}}
                    for level, _ in rows
                ],
            },
        ).json()["ids"]
        closed = [(item, good) for item, (_, good) in zip(ids, rows) if good is not None]
        if closed:
            self._close(closed)
        return ids

    def test_a_cohort_cannot_score_itself(self) -> None:
        """Its own morning is the one pool that was not knowable when the morning ran."""
        ids = self._cohort("2026-09-02", [(float(i), i % 2 == 0) for i in range(20)])

        body = self.client.post("/assess", json={"ids": ids}).json()

        self.assertEqual(body["ranked"], 0, "a cohort alone has no basis, so nothing is ranked")
        assessment = body["appraisals"][0]["assessment"]
        self.assertEqual(assessment["basis"], "none")
        self.assertIsNone(assessment["score"])
        self.assertIn("2026-09-02", assessment["rationale"])
        self.assertIn("cannot score itself", assessment["rationale"])

    def test_an_earlier_cohort_is_what_scores_a_morning(self) -> None:
        self._cohort("2026-09-02", [(float(i), i < 8) for i in range(20)])
        ids = self._cohort("2026-09-04", [(1.0, None)])

        assessment = self.client.post("/assess", json={"ids": ids}).json()["appraisals"][0][
            "assessment"
        ]

        self.assertEqual(assessment["basis"], "observations")
        self.assertEqual(assessment["sample"], 20, "the earlier morning, all of it")
        self.assertIsNotNone(assessment["score"])

    def test_a_later_cohort_does_not_dilute_an_earlier_one(self) -> None:
        """Scoring is per cohort, so two mornings in one batch do not share a pool."""
        self._cohort("2026-09-02", [(float(i), i < 8) for i in range(20)])
        second = self._cohort("2026-09-04", [(float(i), i % 2 == 0) for i in range(20)])
        third = self._cohort("2026-09-05", [(1.0, None)])

        body = self.client.post("/assess", json={"ids": second + third}).json()

        samples = {item["id"]: item["assessment"]["sample"] for item in body["appraisals"]}
        self.assertEqual(samples[second[0]], 20, "the Sep 2 morning only")
        self.assertEqual(samples[third[0]], 40, "both earlier mornings")

    def _timed_cohort(
        self, cohort: str, seen_at: str, rows: list[tuple[float, bool | None]]
    ) -> list[str]:
        ids = self.client.post(
            "/observations",
            json={
                "topic": TOPIC,
                "source": "bot:scanner",
                "source_ref": cohort,
                "observations": [
                    {
                        "content": f"widget spike at {level}",
                        "data": {"level": level},
                        "occurred_at": seen_at,
                    }
                    for level, _ in rows
                ],
            },
        ).json()["ids"]
        closed = [(item, good) for item, (_, good) in zip(ids, rows) if good is not None]
        if closed:
            self._close(closed)
        return ids

    def test_a_later_morning_cannot_score_an_earlier_one(self) -> None:
        """Re-assessing history must not reach forward into mornings that had not happened."""
        self._timed_cohort(
            "2026-09-04", "2026-09-04T13:30:00+00:00", [(float(i), i < 8) for i in range(20)]
        )
        earlier = self._timed_cohort("2026-09-02", "2026-09-02T13:30:00+00:00", [(1.0, None)])

        body = self.client.post("/assess", json={"ids": earlier}).json()

        assessment = body["appraisals"][0]["assessment"]
        self.assertEqual(assessment["basis"], "none")
        self.assertIsNone(assessment["score"])
        self.assertIn("nothing later can score it", assessment["rationale"])

    def test_a_morning_is_scored_on_what_came_before_it(self) -> None:
        self._timed_cohort(
            "2026-09-02", "2026-09-02T13:30:00+00:00", [(float(i), i < 8) for i in range(20)]
        )
        later = self._timed_cohort("2026-09-04", "2026-09-04T13:30:00+00:00", [(1.0, None)])

        assessment = self.client.post("/assess", json={"ids": later}).json()["appraisals"][0][
            "assessment"
        ]

        self.assertEqual(assessment["basis"], "observations")
        self.assertEqual(assessment["sample"], 20)

    def test_evidence_that_cannot_be_placed_in_time_is_not_used(self) -> None:
        """A record with no occurred_at cannot be shown to predate the morning, so it sits out."""
        self._cohort("undated", [(float(i), i < 8) for i in range(20)])
        later = self._timed_cohort("2026-09-04", "2026-09-04T13:30:00+00:00", [(1.0, None)])

        assessment = self.client.post("/assess", json={"ids": later}).json()["appraisals"][0][
            "assessment"
        ]

        self.assertEqual(assessment["basis"], "none")
        self.assertIsNone(assessment["score"])

    def test_an_observation_with_no_cohort_keeps_the_whole_pool(self) -> None:
        """Withholding a cohort is not withholding everything; a loose record is unaffected."""
        self._cohort("2026-09-02", [(float(i), i < 8) for i in range(20)])
        ids = self._post(["widget spike at 1.0"], topic=TOPIC)

        assessment = self.client.post("/assess", json={"ids": ids}).json()["appraisals"][0][
            "assessment"
        ]

        self.assertEqual(assessment["basis"], "observations")
        self.assertEqual(assessment["sample"], 20)

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
        self._morning("2026-09-06", [(0.5, index % 2 == 0) for index in range(10)])
        ids = self._perfect_and_useless()
        self.client.post("/assess", json={"ids": ids, "record": True})

        body = self.client.post("/compare", json={"cohort": "2026-09-07", "top_n": 10}).json()

        sources = {item["source"] for item in body["rankers"]}
        self.assertIn("iris:shadow-2", sources)
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
        self.assertEqual(sources, {"bot:evaluator", "iris:shadow-2"})

    def test_a_ranker_that_gave_one_score_expressed_no_order(self) -> None:
        """A flat score is not a ranking, so it cannot lead and its top N is a slice."""
        self._morning("flat", [(0.5, index % 2 == 0) for index in range(20)])

        body = self.client.post("/compare", json={"cohort": "flat", "top_n": 5}).json()

        bot = [item for item in body["rankers"] if item["source"] == "bot:evaluator"][0]
        self.assertEqual(bot["distinct_scores"], 1)
        self.assertIsNone(body["leader"], "no order means no leader, whatever the hit rate")
        self.assertTrue(any("expressed no order" in note for note in body["notes"]))

    def test_coarse_scores_are_reported_against_what_was_scored(self) -> None:
        """Two values over forty candidates orders almost nothing, however it scores."""
        rows = [(0.9, index % 3 == 0) for index in range(20)]
        rows += [(0.1, index % 3 == 0) for index in range(20)]
        self._morning("coarse", rows)

        body = self.client.post("/compare", json={"cohort": "coarse", "top_n": 10}).json()

        bot = [item for item in body["rankers"] if item["source"] == "bot:evaluator"][0]
        self.assertEqual(bot["scored"], 40)
        self.assertEqual(bot["distinct_scores"], 2)
        self.assertTrue(any("only 2 distinct scores" in note for note in body["notes"]))

    def _featured_morning(self, cohort: str, rows: list[tuple[float, float, bool]]) -> list[str]:
        ids = self.client.post(
            "/observations",
            json={
                "topic": TOPIC,
                "source": "bot:scanner",
                "source_ref": cohort,
                "observations": [
                    {
                        "content": f"candidate at level {level}",
                        "data": {"level": level},
                        "decision": {
                            "content": f"bot confidence {own}",
                            "source": "bot:evaluator",
                            "score": own,
                        },
                    }
                    for level, own, _ in rows
                ],
            },
        ).json()["ids"]
        self.client.post(
            "/outcomes",
            json={
                "source": "bot:broker",
                "outcomes": [
                    {"observation_id": item, "content": "closed", "favourable": good}
                    for item, (_, _, good) in zip(ids, rows)
                ],
            },
        )
        return ids

    def test_a_lead_inside_the_noise_names_no_leader(self) -> None:
        """The first real morning put the two a few hits apart on fifty. That is not a win."""
        self._featured_morning(
            "2026-09-02",
            [(float(index), index / 20.0, index % 3 == 0) for index in range(20)],
        )
        ids = self._featured_morning(
            "2026-09-04",
            [(float(index), index / 60.0, index % 4 == 0) for index in range(60)],
        )
        self.client.post("/assess", json={"ids": ids, "record": True})

        body = self.client.post("/compare", json={"cohort": "2026-09-04", "top_n": 50}).json()

        self.assertEqual(len(body["rankers"]), 2, "both rankers took part")
        self.assertIsNone(body["leader"])
        self.assertTrue(any("does not name a leader" in note for note in body["notes"]))
        self.assertIn("No leader", body["summary"])

    def test_topping_a_field_of_one_is_not_leading(self) -> None:
        """A sole ranker below the base rate has beaten nothing, least of all the coin."""
        self._morning("2026-09-02", [(index / 20.0, index % 3 == 0) for index in range(20)])

        body = self.client.post("/compare", json={"cohort": "2026-09-02", "top_n": 10}).json()

        bot = [item for item in body["rankers"] if item["source"] == "bot:evaluator"][0]
        self.assertLessEqual(bot["lift"], 0.0)
        self.assertIsNone(body["leader"])
        self.assertTrue(any("does not clear the" in note for note in body["notes"]))

    def test_a_clear_win_is_still_called(self) -> None:
        """Withholding a leader on noise must not withhold one on a real gap."""
        self._morning("2026-09-02", [(0.5, index % 2 == 0) for index in range(20)])
        self._perfect_and_useless()

        body = self.client.post("/compare", json={"cohort": "2026-09-07", "top_n": 10}).json()

        self.assertEqual(body["leader"], "bot:evaluator")
        self.assertIn("Leader: bot:evaluator", body["summary"])

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


class ApprovedRuleTests(unittest.TestCase):
    """An accepted hypothesis is defined by the observations it was proven on."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = _Service(Path(self._tmp.name))
        self.client = TestClient(create_app(self.service))
        self.tracker = self.service.knowledge_review.tracker

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _observe(self, rel_vol: float, favourable: bool, source_ref: str = "history") -> str:
        ids = self.client.post(
            "/observations",
            json={
                "topic": TOPIC,
                "source": "bot:scanner",
                "source_ref": source_ref,
                "observations": [
                    {"content": f"SYM rel vol {rel_vol}", "data": {"rel_vol": rel_vol}}
                ],
            },
        ).json()["ids"]
        self.client.post(
            "/outcomes",
            json={
                "source": "bot:broker",
                "outcomes": [
                    {"observation_id": ids[0], "content": "closed", "favourable": favourable}
                ],
            },
        )
        return ids[0]

    def _accepted_rule(self, band: list[float]):
        hypothesis = self.tracker.propose(
            "High relative volume predicts outperformance.", topic=TOPIC, source="analysis"
        )
        for rel_vol in band:
            observation_id = self._observe(rel_vol, True)
            outcome = self.service.knowledge.outcomes_for([observation_id])[observation_id]
            self.service.knowledge.add_evidence(hypothesis.id, outcome, supports=True)
        self.service.knowledge_review.refresh()
        self.service.knowledge_review.approve(hypothesis.id, approved_by="henry")
        return hypothesis

    def _assess(self, rel_vol: float) -> dict:
        ids = self.client.post(
            "/observations",
            json={
                "topic": TOPIC,
                "source": "bot:scanner",
                "observations": [
                    {"content": f"CAND rel vol {rel_vol}", "data": {"rel_vol": rel_vol}}
                ],
            },
        ).json()["ids"]
        return self.client.post("/assess", json={"ids": ids}).json()["appraisals"][0]

    def test_a_candidate_inside_the_proven_range_is_covered_by_the_rule(self) -> None:
        self._accepted_rule([3.0, 3.5, 4.0, 4.5, 5.0])

        assessment = self._assess(4.0)["assessment"]

        self.assertEqual(assessment["basis"], "hypothesis")
        self.assertEqual(len(assessment["rules"]), 1)
        self.assertEqual(assessment["rules"][0]["matched_on"], ["rel_vol"])
        self.assertIn("High relative volume", assessment["rules"][0]["content"])

    def test_a_candidate_outside_the_proven_range_is_not(self) -> None:
        self._accepted_rule([3.0, 3.5, 4.0, 4.5, 5.0])

        assessment = self._assess(1.1)["assessment"]

        self.assertNotEqual(assessment["basis"], "hypothesis")
        self.assertEqual(assessment["rules"], [])

    def test_the_rule_carries_the_evidence_that_earned_it(self) -> None:
        self._accepted_rule([3.0, 3.5, 4.0, 4.5, 5.0])

        rule = self._assess(4.0)["assessment"]["rules"][0]

        self.assertEqual(rule["supporting"], 5)
        self.assertEqual(rule["contradicting"], 0)

    def test_an_unapproved_hypothesis_covers_nothing(self) -> None:
        """Only accepted rules apply; supported is not approved."""
        hypothesis = self.tracker.propose("An idea.", topic=TOPIC, source="analysis")
        for rel_vol in (3.0, 3.5, 4.0, 4.5, 5.0):
            observation_id = self._observe(rel_vol, True)
            outcome = self.service.knowledge.outcomes_for([observation_id])[observation_id]
            self.service.knowledge.add_evidence(hypothesis.id, outcome, supports=True)
        self.service.knowledge_review.refresh()

        assessment = self._assess(4.0)["assessment"]

        self.assertEqual(self.service.knowledge.records.get(hypothesis.id).status,
                         MemoryStatus.SUPPORTED)
        self.assertEqual(assessment["rules"], [])

    def test_the_rule_never_supplies_the_number(self) -> None:
        """The score stays counted from outcomes; a rule is rationale, not arithmetic."""
        self._accessed = self._accepted_rule([3.0, 3.5, 4.0, 4.5, 5.0])
        for rel_vol in (3.1, 3.6, 4.1, 4.6, 4.9):
            self._observe(rel_vol, False)

        assessment = self._assess(4.0)["assessment"]

        self.assertEqual(assessment["basis"], "hypothesis")
        self.assertLess(assessment["score"], 1.0, "contradicting outcomes must still pull it down")
        self.assertIn("counted from outcomes, not from the rule", assessment["rationale"])

    def test_coverage_does_not_make_it_binding(self) -> None:
        self._accepted_rule([3.0, 3.5, 4.0, 4.5, 5.0])

        appraisal = self._assess(4.0)

        self.assertFalse(appraisal["binding"])
        self.assertEqual(appraisal["contract"], "shadow-2")

    def test_a_rule_with_no_numeric_evidence_is_skipped(self) -> None:
        hypothesis = self.tracker.propose("Vague idea.", topic=TOPIC, source="analysis")
        for index in range(5):
            record = self.service.knowledge.records.add(
                MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, source="x", content=f"o{index}")
            )
            self.service.knowledge.add_evidence(hypothesis.id, record, supports=True)
        self.service.knowledge_review.refresh()
        self.service.knowledge_review.approve(hypothesis.id, approved_by="henry")

        self.assertEqual(self._assess(4.0)["assessment"]["rules"], [])

    def test_the_method_is_reported_so_appraisals_can_be_told_apart(self) -> None:
        self._observe(2.0, True)

        assessment = self._assess(2.0)["assessment"]

        self.assertIn(assessment["method"], {"feature-knn", "text-similarity"})
