# File: core/tests/test_workflows.py

"""Workflows on the tool layer (8.3): chains of tools stored as data, with explicit failure handling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.audit.stream import AuditCategory, AuditStream
from core.workflows.models import WorkflowDefinition, WorkflowError, WorkflowStep, WorkflowTrigger, render_arguments
from core.workflows.runner import TIMED_OUT, WorkflowRunner
from core.workflows.service import WorkflowService


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_times: dict[str, int] = {}
        self.confirm: set[str] = set()
        self.versions: dict[str, str] = {"fetch": "1", "observe": "1", "notify": "1"}

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        remaining = self.fail_times.get(name, 0)
        if remaining:
            self.fail_times[name] = remaining - 1
            return {"status": "failed", "error": "boom", "message": f"{name} exploded"}
        if name in self.confirm:
            return {"status": "pending_confirmation", "error": "confirmation_required", "message": "Awaiting confirmation."}
        if name == "sleepy":
            import time

            time.sleep(2.0)
        return {
            "status": "success",
            "message": f"{name} done",
            "results": [{"kind": "text", "source": {"name": name}, "data": {"text": f"{name} text", "url": arguments.get("url")}}],
        }

    def preview(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "preview", "message": f"Would run {name} with {arguments}", "preview": None}

    def version(self, name: str) -> str | None:
        return self.versions.get(name)


def _workflow(*steps: WorkflowStep, name: str = "Morning", trigger: WorkflowTrigger | None = None) -> WorkflowDefinition:
    return WorkflowDefinition(name=name, steps=tuple(steps), trigger=trigger or WorkflowTrigger(), id="wf1")


class ModelTests(unittest.TestCase):
    def test_arguments_reach_across_steps_and_the_trigger(self) -> None:
        context = {"trigger": {"url": "https://example.com"}, "steps": {"fetch": {"first": {"text": "hello"}, "status": "success"}}}
        rendered = render_arguments({"url": "{{ trigger.url }}", "content": "Got: {{steps.fetch.first.text}}", "n": 3}, context)
        self.assertEqual(rendered, {"url": "https://example.com", "content": "Got: hello", "n": 3})

    def test_a_reference_to_nothing_is_an_error_not_an_empty_string(self) -> None:
        with self.assertRaises(WorkflowError):
            render_arguments({"x": "{{steps.nope.first}}"}, {"trigger": {}, "steps": {}})

    def test_a_workflow_is_checked_when_it_is_built(self) -> None:
        with self.assertRaises(WorkflowError):
            WorkflowDefinition(name="Empty", steps=())
        with self.assertRaises(WorkflowError):
            WorkflowStep(id="a", tool="x", on_failure="explode")
        with self.assertRaises(WorkflowError):
            WorkflowTrigger(kind="schedule")

    def test_a_definition_round_trips_through_json(self) -> None:
        definition = _workflow(WorkflowStep(id="a", tool="fetch", arguments={"url": "u"}), WorkflowStep(id="b", tool="observe", approve=True))
        restored = WorkflowDefinition.from_json(json.loads(json.dumps(definition.to_json())))
        self.assertEqual(restored.to_json(), definition.to_json())


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = FakeTools()
        self.runner = WorkflowRunner(self.tools.invoke, preview=self.tools.preview, tool_version=self.tools.version, sleep=lambda _seconds: None)

    def test_steps_run_in_order_and_feed_each_other(self) -> None:
        """detect -> gather -> analyze -> store -> notify, with each step reading the last."""
        workflow = _workflow(
            WorkflowStep(id="fetch", tool="fetch", arguments={"url": "{{trigger.url}}"}, save_as="page"),
            WorkflowStep(id="store", tool="observe", arguments={"content": "{{page.first.text}}"}),
            WorkflowStep(id="tell", tool="notify", arguments={"body": "stored {{steps.store.status}}"}),
        )
        run = self.runner.run(workflow, payload={"url": "https://example.com"})

        self.assertEqual(run.status, "success", run.note)
        self.assertEqual([name for name, _ in self.tools.calls], ["fetch", "observe", "notify"])
        self.assertEqual(self.tools.calls[0][1], {"url": "https://example.com"})
        self.assertEqual(self.tools.calls[1][1], {"content": "fetch text"})
        self.assertEqual(self.tools.calls[2][1], {"body": "stored success"})

    def test_a_failure_stops_the_chain_by_default(self) -> None:
        self.tools.fail_times["fetch"] = 1
        run = self.runner.run(_workflow(WorkflowStep(id="a", tool="fetch"), WorkflowStep(id="b", tool="notify")))
        self.assertEqual(run.status, "failed")
        self.assertEqual([name for name, _ in self.tools.calls], ["fetch"])
        self.assertIn("Stopped at step a", run.note)

    def test_continue_marks_the_run_partial_and_carries_on(self) -> None:
        self.tools.fail_times["fetch"] = 1
        run = self.runner.run(_workflow(WorkflowStep(id="a", tool="fetch", on_failure="continue"), WorkflowStep(id="b", tool="notify")))
        self.assertEqual(run.status, "partial")
        self.assertEqual([name for name, _ in self.tools.calls], ["fetch", "notify"])

    def test_retry_tries_again_and_records_the_attempts(self) -> None:
        self.tools.fail_times["fetch"] = 2
        run = self.runner.run(_workflow(WorkflowStep(id="a", tool="fetch", on_failure="retry", retries=3)))
        self.assertEqual(run.status, "success")
        self.assertEqual(run.steps[0].attempts, 3)

    def test_retries_run_out(self) -> None:
        self.tools.fail_times["fetch"] = 5
        run = self.runner.run(_workflow(WorkflowStep(id="a", tool="fetch", on_failure="retry", retries=1)))
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.steps[0].attempts, 2)

    def test_a_slow_step_times_out(self) -> None:
        run = self.runner.run(_workflow(WorkflowStep(id="a", tool="sleepy", timeout_seconds=0.2)))
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.steps[0].error, TIMED_OUT)

    def test_an_approval_step_pauses_the_run_until_a_person_says_so(self) -> None:
        workflow = _workflow(WorkflowStep(id="a", tool="fetch"), WorkflowStep(id="b", tool="observe", approve=True), WorkflowStep(id="c", tool="notify"))
        paused = self.runner.run(workflow)

        self.assertEqual(paused.status, "awaiting_approval")
        self.assertEqual(paused.next_step, "b")
        self.assertEqual([name for name, _ in self.tools.calls], ["fetch"])

        resumed = self.runner.run(workflow, resume=paused, approved=True)
        self.assertEqual(resumed.status, "success", resumed.note)
        self.assertEqual(resumed.id, paused.id)
        self.assertEqual([name for name, _ in self.tools.calls], ["fetch", "observe", "notify"])

    def test_a_tool_that_asks_for_confirmation_pauses_the_run_too(self) -> None:
        self.tools.confirm.add("observe")
        run = self.runner.run(_workflow(WorkflowStep(id="a", tool="observe")))
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.next_step, "a")

    def test_a_dry_run_runs_nothing(self) -> None:
        run = self.runner.run(_workflow(WorkflowStep(id="a", tool="fetch"), WorkflowStep(id="b", tool="observe", approve=True)), dry_run=True)
        self.assertEqual(run.status, "dry_run")
        self.assertEqual(self.tools.calls, [])
        self.assertEqual([step.status for step in run.steps], ["preview", "preview"])

    def test_a_changed_tool_blocks_the_run_rather_than_changing_its_meaning(self) -> None:
        workflow = WorkflowDefinition(name="Pinned", steps=(WorkflowStep(id="a", tool="fetch"),), tool_versions={"fetch": "1"}, id="wf2")
        self.tools.versions["fetch"] = "2"
        run = self.runner.run(workflow)
        self.assertEqual(run.status, "blocked")
        self.assertIn("fetch 1 -> 2", run.note)
        self.assertEqual(self.tools.calls, [])


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.tools = FakeTools()
        self.stream = AuditStream(self.root / "audit")
        self.notified: list[Any] = []
        self.service = WorkflowService(
            self.root / "Workflows",
            WorkflowRunner(self.tools.invoke, preview=self.tools.preview, tool_version=self.tools.version, sleep=lambda _s: None),
            tool_version=self.tools.version,
            audit=self.stream,
            notify=self.notified.append,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_workflow_is_a_file_that_pins_its_tools(self) -> None:
        saved = self.service.save(_workflow(WorkflowStep(id="a", tool="fetch"), WorkflowStep(id="b", tool="observe")))
        path = self.root / "Workflows" / "wf1.json"
        self.assertTrue(path.exists())
        self.assertEqual(saved.tool_versions, {"fetch": "1", "observe": "1"})
        reopened = WorkflowService(self.root / "Workflows", self.service.runner)
        self.assertEqual([item.id for item in reopened.definitions()], ["wf1"])

    def test_editing_bumps_the_version(self) -> None:
        first = self.service.save(_workflow(WorkflowStep(id="a", tool="fetch")))
        second = self.service.save(_workflow(WorkflowStep(id="a", tool="fetch"), WorkflowStep(id="b", tool="notify")))
        self.assertEqual((first.version, second.version), (1, 2))

    def test_a_file_edited_by_hand_is_picked_up(self) -> None:
        self.service.save(_workflow(WorkflowStep(id="a", tool="fetch")))
        path = self.root / "Workflows" / "wf1.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["name"] = "Renamed"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        self.assertTrue(self.service.reload_if_changed())
        self.assertEqual(self.service.get("wf1").name, "Renamed")

    def test_rebase_accepts_the_new_tool_versions(self) -> None:
        self.service.save(_workflow(WorkflowStep(id="a", tool="fetch")))
        self.tools.versions["fetch"] = "2"
        self.assertEqual(self.service.run("wf1").status, "blocked")
        self.service.rebase("wf1")
        self.assertEqual(self.service.run("wf1").status, "success")

    def test_runs_are_kept_and_a_bad_one_is_reported(self) -> None:
        self.service.save(_workflow(WorkflowStep(id="a", tool="fetch")))
        self.tools.fail_times["fetch"] = 1
        run = self.service.run("wf1")
        self.assertEqual(run.status, "failed")
        self.assertEqual([item.id for item in self.service.runs()], [run.id])
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.stream.read(category=AuditCategory.ACTION)[-1].event, "workflow")

    def test_approval_resumes_from_the_stored_run(self) -> None:
        self.service.save(_workflow(WorkflowStep(id="a", tool="fetch"), WorkflowStep(id="b", tool="observe", approve=True)))
        paused = self.service.run("wf1")
        self.assertEqual([item.id for item in self.service.awaiting()], [paused.id])
        finished = self.service.approve(paused.id)
        self.assertEqual(finished.status, "success", finished.note)
        self.assertEqual(self.service.awaiting(), [])

    def test_a_watcher_trigger_finds_its_workflows(self) -> None:
        self.service.save(_workflow(WorkflowStep(id="a", tool="notify"), trigger=WorkflowTrigger(kind="watcher", watcher="disk_free_below")))
        self.assertEqual([item.id for item in self.service.for_watcher("abc123", "disk_free_below")], ["wf1"])
        self.assertEqual(self.service.for_watcher("abc123", "cpu_percent_above"), [])

    def test_a_disabled_workflow_does_not_fire_from_a_trigger_but_can_be_run_by_hand(self) -> None:
        self.service.save(_workflow(WorkflowStep(id="a", tool="notify")))
        self.service.set_enabled("wf1", False)
        self.assertIsNone(self.service.run("wf1", trigger="schedule"))
        self.assertIsNotNone(self.service.run("wf1"))

    def test_a_stopped_iris_blocks_workflows(self) -> None:
        self.service.save(_workflow(WorkflowStep(id="a", tool="notify")))
        self.service.halted = True
        self.assertEqual(self.service.run("wf1").status, "blocked")
        self.assertEqual(self.tools.calls, [])


class TriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.tools = FakeTools()
        self.service = WorkflowService(
            self.root / "Workflows",
            WorkflowRunner(self.tools.invoke, tool_version=self.tools.version, sleep=lambda _s: None),
            tool_version=self.tools.version,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_scheduled_workflow_becomes_a_scheduled_job_and_leaves_with_it(self) -> None:
        #! @allow-local-import
        from core.scheduler.jobs import build_jobs
        #! @allow-local-import
        from core.scheduler.service import ScheduleService

        schedules = ScheduleService(self.root / "schedules.json", self.root / "state.json", build_jobs(workflows=self.service))
        self.service.save(_workflow(WorkflowStep(id="a", tool="notify"), trigger=WorkflowTrigger(kind="schedule", cron="0 7 * * *")))

        self.assertEqual(self.service.sync_schedules(schedules), ["added workflow-wf1"])
        job = schedules.get("workflow-wf1")
        self.assertEqual(job.cron, "0 7 * * *")
        self.assertEqual(job.params, {"workflow_id": "wf1"})

        result = schedules.run("workflow-wf1")
        self.assertTrue(result.ok, result.summary)
        self.assertEqual([name for name, _ in self.tools.calls], ["notify"])

        self.service.remove("wf1")
        self.assertEqual(self.service.sync_schedules(schedules), ["removed workflow-wf1"])
        self.assertIsNone(schedules.get("workflow-wf1"))

    def test_a_watcher_alert_runs_its_workflow_with_the_alert_as_payload(self) -> None:
        #! @allow-local-import
        from core.watchers.models import Notification

        self.service.save(
            _workflow(
                WorkflowStep(id="a", tool="notify", arguments={"body": "{{trigger.title}}: {{trigger.body}}"}),
                trigger=WorkflowTrigger(kind="watcher", watcher="disk_free_below"),
            )
        )
        listener = self.service.watcher_listener()
        listener(Notification(watcher_id="w1", title="E: is low", body="12 GB free", kind="disk_free_below"))
        listener(Notification(watcher_id="w1", title="E: is low — cleared", body="40 GB free", kind="disk_free_below", cleared=True))

        self.assertEqual(self.tools.calls, [("notify", {"body": "E: is low: 12 GB free"})])
        self.assertEqual(self.service.runs()[0].trigger, "watcher")


class BootstrapTests(unittest.TestCase):
    def test_desktop_and_host_declare_the_same_native_tools(self) -> None:
        #! @allow-local-import
        from core.actions.bootstrap import build_action_layer

        with tempfile.TemporaryDirectory() as tmp:
            layer = build_action_layer({"assistant_name": "Iris"}, catalog=None, audit_folder=Path(tmp) / "audit")
        names = set(layer.tool_registry.names())
        for expected in ("open_file", "launch_application", "fetch_web_page", "disk_usage", "update_config", "update_profile"):
            self.assertIn(expected, names)
        self.assertEqual(layer.executor.registry, layer.action_registry)


if __name__ == "__main__":
    unittest.main()
