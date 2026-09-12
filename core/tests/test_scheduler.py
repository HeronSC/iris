# File: core/tests/test_scheduler.py

"""Hypotheses re-appraised on a schedule, and the trading watchers (13.1, 8.2)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.audit.stream import AuditCategory, AuditStream
from core.knowledge import KnowledgeGraph, MemoryKind, MemoryRecord, MemoryStatus
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.review import KnowledgeReviewWorkflow
from core.scheduler.jobs import HYPOTHESIS_REVIEW, build_jobs
from core.scheduler.models import JobDefinition, JobResult
from core.scheduler.service import ScheduleService
from core.storage.sqlite_database import SQLiteDatabase
from core.watchers.knowledge_checks import KNOWLEDGE_KINDS
from core.watchers.models import WatcherContext

TOPIC = "trading/candidates"


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


class ScheduleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.stream = AuditStream(self.root)
        self.calls: list[dict[str, Any]] = []
        self.result = JobResult(ok=True, summary="did the thing")
        self.service = ScheduleService(
            self.root / "schedules.json",
            self.root / "state.json",
            {"demo": self._job},
            audit=self.stream,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _job(self, params: dict[str, Any]) -> JobResult:
        self.calls.append(params)
        return self.result

    def test_a_job_is_data_and_survives_a_restart(self) -> None:
        """Watchers settled this: definitions on disk, rescheduled at startup."""
        self.service.add(JobDefinition(job="demo", params={"topic": TOPIC}, interval_seconds=3600, id="demo-1"))

        reopened = ScheduleService(self.root / "schedules.json", self.root / "state.json", {"demo": self._job})
        self.assertEqual([item.id for item in reopened.definitions()], ["demo-1"])
        self.assertEqual(reopened.get("demo-1").params, {"topic": TOPIC})

    def test_running_records_the_outcome_and_audits_it(self) -> None:
        self.service.add(JobDefinition(job="demo", interval_seconds=60, id="demo-1"))
        result = self.service.run("demo-1")

        self.assertTrue(result.ok)
        state = self.service.state("demo-1")
        self.assertEqual(state.runs, 1)
        self.assertEqual(state.last_summary, "did the thing")
        events = self.stream.read(category=AuditCategory.SCHEDULE)
        self.assertEqual(events[0].event, "demo")
        self.assertEqual(events[0].status, "success")

    def test_a_job_that_raises_is_recorded_not_swallowed(self) -> None:
        def explode(_params: dict[str, Any]) -> JobResult:
            raise RuntimeError("the database is gone")

        self.service.jobs["boom"] = explode
        self.service.add(JobDefinition(job="boom", interval_seconds=60, id="boom-1"))
        result = self.service.run("boom-1")

        self.assertFalse(result.ok)
        self.assertIn("the database is gone", result.summary)
        self.assertEqual(self.service.state("boom-1").failures, 1)
        self.assertEqual(self.stream.read(category=AuditCategory.SCHEDULE)[0].status, "failed")

    def test_an_unknown_job_is_refused_when_it_is_added(self) -> None:
        with self.assertRaises(ValueError):
            self.service.add(JobDefinition(job="nonsense", interval_seconds=60))

    def test_ensure_does_not_add_the_same_job_twice(self) -> None:
        first = self.service.ensure(JobDefinition(job="demo", cron="0 6 * * *", id="demo-1"))
        second = self.service.ensure(JobDefinition(job="demo", cron="0 7 * * *", id="demo-2"))
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.service.definitions()), 1)

    def test_a_cron_expression_has_five_fields(self) -> None:
        with self.assertRaises(ValueError):
            JobDefinition(job="demo", cron="0 6 * *")

    def test_a_disabled_job_is_left_out_of_a_sweep(self) -> None:
        self.service.add(JobDefinition(job="demo", interval_seconds=60, id="demo-1"))
        self.service.set_enabled("demo-1", False)
        self.service.run_all()
        self.assertEqual(self.calls, [])


class HypothesisReviewJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.graph = KnowledgeGraph(SQLiteDatabase(root / "knowledge.db"))
        self.tracker = HypothesisTracker(self.graph)
        self.review = KnowledgeReviewWorkflow(self.tracker)
        self.job = build_jobs(review=self.review)[HYPOTHESIS_REVIEW]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _supported_hypothesis(self) -> str:
        hypothesis = self.tracker.propose("Gaps over 4% fade by noon", topic=TOPIC, source="henry")
        self.tracker.begin_testing(hypothesis.id)
        for index in range(5):
            evidence = self.graph.records.add(
                MemoryRecord(kind=MemoryKind.OBSERVATION, topic=TOPIC, content=f"SYM{index} faded", source="bot")
            )
            self.graph.add_evidence(hypothesis.id, evidence, supports=True, note="test")
        return hypothesis.id

    def test_the_schedule_moves_what_a_person_would_have_had_to(self) -> None:
        """13.1: re-evaluate on a schedule instead of only when someone files evidence."""
        hypothesis_id = self._supported_hypothesis()
        self.graph.records.set_status(hypothesis_id, MemoryStatus.TESTING)

        result = self.job({"topic": TOPIC})

        self.assertTrue(result.ok)
        self.assertEqual(self.graph.records.get(hypothesis_id).status, MemoryStatus.SUPPORTED)
        self.assertIn(hypothesis_id, result.data["newly_waiting"])
        self.assertTrue(result.notify)
        self.assertIn("evidence to be accepted", result.summary)

    def test_a_quiet_pass_says_so_without_shouting(self) -> None:
        result = self.job({"topic": TOPIC})
        self.assertTrue(result.ok)
        self.assertFalse(result.notify)
        self.assertIn("Nothing moved", result.summary)

    def test_the_schedule_never_accepts_a_hypothesis_by_itself(self) -> None:
        """Accepted means "reason with this", and only a person can say that."""
        hypothesis_id = self._supported_hypothesis()
        self.job({"topic": TOPIC})
        self.assertEqual(self.graph.records.get(hypothesis_id).status, MemoryStatus.SUPPORTED)


class TradingWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.graph = KnowledgeGraph(SQLiteDatabase(root / "knowledge.db"))
        self.context = WatcherContext(knowledge=self.graph)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, kind: str, params: dict[str, Any]) -> Any:
        return KNOWLEDGE_KINDS[kind].run(params, None, self.context)

    def _observation(self, *, hours_ago: float, content: str = "SYM entered the list") -> MemoryRecord:
        return self.graph.records.add(
            MemoryRecord(
                kind=MemoryKind.OBSERVATION,
                topic=TOPIC,
                content=content,
                source="bot:scanner",
                occurred_at=_hours_ago(hours_ago),
            )
        )

    def test_a_candidate_list_that_stopped_arriving_trips(self) -> None:
        self._observation(hours_ago=30)
        result = self._run("no_new_records", {"topic": TOPIC, "hours": 24})
        self.assertTrue(result.triggered)
        self.assertIn("expected one every 24 h", result.summary)

    def test_a_list_that_is_still_arriving_does_not(self) -> None:
        self._observation(hours_ago=1)
        self.assertFalse(self._run("no_new_records", {"topic": TOPIC, "hours": 24}).triggered)

    def test_a_topic_that_never_had_anything_trips(self) -> None:
        self.assertTrue(self._run("no_new_records", {"topic": TOPIC, "hours": 24}).triggered)

    def test_outcomes_that_never_arrive_trip(self) -> None:
        self._observation(hours_ago=30)
        self._observation(hours_ago=30, content="SYM2 entered the list")
        result = self._run("outcomes_overdue", {"topic": TOPIC, "hours": 24})
        self.assertTrue(result.triggered)
        self.assertEqual(result.value, 2)

    def test_a_closed_observation_is_not_overdue(self) -> None:
        observation = self._observation(hours_ago=30)
        self.graph.record_outcome(
            observation.id,
            MemoryRecord(kind=MemoryKind.OUTCOME, topic=TOPIC, content="faded", source="bot:scanner"),
        )
        self.assertFalse(self._run("outcomes_overdue", {"topic": TOPIC, "hours": 24}).triggered)

    def test_assessments_that_stop_arriving_trip(self) -> None:
        self.graph.records.add(
            MemoryRecord(
                kind=MemoryKind.DECISION,
                topic=TOPIC,
                content="ranked 3 candidates",
                source="iris:assessment/1",
                occurred_at=_hours_ago(30),
            )
        )
        self.assertTrue(self._run("no_assessments", {"topic": TOPIC, "hours": 24}).triggered)
        self.assertFalse(self._run("no_assessments", {"topic": TOPIC, "hours": 48}).triggered)

    def test_the_bots_own_decisions_do_not_count_as_iris_scoring(self) -> None:
        """Comparison is the point: Iris's score has to be Iris's."""
        self.graph.records.add(
            MemoryRecord(
                kind=MemoryKind.DECISION,
                topic=TOPIC,
                content="bot picked SYM1",
                source="bot:scanner",
                occurred_at=_hours_ago(1),
            )
        )
        self.assertTrue(self._run("no_assessments", {"topic": TOPIC, "hours": 24}).triggered)

    def test_a_watcher_without_the_graph_says_so(self) -> None:
        with self.assertRaises(ValueError):
            KNOWLEDGE_KINDS["no_new_records"].run({"topic": TOPIC}, None, WatcherContext())

    def test_a_watcher_without_a_topic_says_so(self) -> None:
        with self.assertRaises(ValueError):
            self._run("no_new_records", {})


if __name__ == "__main__":
    unittest.main()
