# File: core/workflows/runner.py

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from core.workflows.models import (
    StepOutcome,
    WorkflowDefinition,
    WorkflowError,
    WorkflowRun,
    WorkflowStep,
    render_arguments,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

Invoker = Callable[[str, dict[str, Any]], dict[str, Any]]

Previewer = Callable[[str, dict[str, Any]], dict[str, Any]]

RETRY_BACKOFF_SECONDS = (1.0, 3.0, 10.0)

TIMED_OUT = "timed_out"


def _with_timeout(call: Callable[[], dict[str, Any]], seconds: float) -> dict[str, Any]:
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["outcome"] = call()
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            box["outcome"] = {"status": "failed", "error": type(error).__name__, "message": str(error)}

    thread = threading.Thread(target=work, name="workflow-step", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return {"status": "failed", "error": TIMED_OUT, "message": f"Step did not finish within {seconds:g} s"}
    return box.get("outcome") or {"status": "failed", "error": "no_outcome", "message": "The step produced nothing"}


class WorkflowRunner:
    def __init__(
        self,
        invoke: Invoker,
        *,
        preview: Previewer | None = None,
        confirm: Invoker | None = None,
        tool_version: Callable[[str], str | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.invoke = invoke
        self.preview = preview
        self.confirm = confirm
        self.tool_version = tool_version
        self.sleep = sleep

    def version_drift(self, workflow: WorkflowDefinition) -> dict[str, tuple[str, str | None]]:
        if self.tool_version is None:
            return {}
        drift: dict[str, tuple[str, str | None]] = {}
        for tool, pinned in workflow.tool_versions.items():
            current = self.tool_version(tool)
            if current != pinned:
                drift[tool] = (pinned, current)
        return drift

    def run(
        self,
        workflow: WorkflowDefinition,
        *,
        payload: dict[str, Any] | None = None,
        trigger: str = "manual",
        dry_run: bool = False,
        resume: WorkflowRun | None = None,
        approved: bool = False,
    ) -> WorkflowRun:
        drift = self.version_drift(workflow)
        if drift and not dry_run:
            changed = ", ".join(f"{tool} {pinned} -> {current or 'gone'}" for tool, (pinned, current) in drift.items())
            return WorkflowRun(
                workflow_id=workflow.id,
                workflow_name=workflow.name,
                version=workflow.version,
                status="blocked",
                trigger=trigger,
                payload=dict(payload or {}),
                finished_at=utc_now_iso(),
                note=f"Tools changed since this workflow was saved: {changed}. /workflow rebase {workflow.id} accepts them.",
            )

        context: dict[str, Any] = {"trigger": dict(payload or {}), "steps": {}}
        outcomes: list[StepOutcome] = []
        run_id = resume.id if resume is not None else None
        started_at = resume.started_at if resume is not None else utc_now_iso()
        start_from = 0
        if resume is not None:
            outcomes = list(resume.steps)
            for outcome in outcomes:
                context["steps"][outcome.step_id] = self._context_entry(outcome)
                saved = next((step.save_as for step in workflow.steps if step.id == outcome.step_id and step.save_as), None)
                if saved:
                    context[saved] = context["steps"][outcome.step_id]
            start_from = next((index for index, step in enumerate(workflow.steps) if step.id == resume.next_step), len(workflow.steps))

        status = "success"
        note = ""
        next_step: str | None = None
        for index in range(start_from, len(workflow.steps)):
            step = workflow.steps[index]
            approved_here = approved and resume is not None and resume.next_step == step.id
            if step.approve and not approved_here and not dry_run:
                status = "awaiting_approval"
                next_step = step.id
                note = f"Step {step.id} ({step.tool}) needs a person's approval. /workflow approve <run> runs it."
                break
            try:
                arguments = render_arguments(step.arguments, context)
            except WorkflowError as error:
                outcome = StepOutcome(step_id=step.id, tool=step.tool, status="failed", error="bad_reference", message=str(error), arguments=dict(step.arguments))
                outcomes.append(outcome)
                status = "failed"
                note = str(error)
                break
            outcome = self._preview_step(step, arguments) if dry_run else self._run_step(step, arguments, confirmed=approved_here)
            outcomes.append(outcome)
            context["steps"][step.id] = self._context_entry(outcome)
            if step.save_as:
                context[step.save_as] = context["steps"][step.id]
            if outcome.status == "success" or dry_run:
                continue
            if outcome.status == "pending_confirmation":
                status = "awaiting_approval"
                next_step = step.id
                note = f"Step {step.id} ({step.tool}) asked for confirmation. /workflow approve <run> gives it."
                outcomes.pop()
                break
            if step.on_failure == "continue":
                status = "partial"
                note = f"Step {step.id} failed ({outcome.error or outcome.status}) and the workflow carried on."
                continue
            status = "failed"
            note = f"Stopped at step {step.id}: {outcome.message or outcome.error or outcome.status}"
            break

        if dry_run:
            status = "dry_run"
            note = "Nothing ran; this is what each step would do."
        finished = status != "awaiting_approval"
        return WorkflowRun(
            id=run_id or WorkflowRun.__dataclass_fields__["id"].default_factory(),
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            version=workflow.version,
            status=status,
            started_at=started_at,
            finished_at=utc_now_iso() if finished else None,
            trigger=trigger,
            payload=dict(payload or (resume.payload if resume is not None else {})),
            steps=tuple(outcomes),
            next_step=next_step,
            note=note,
            dry_run=dry_run,
        )

    def _run_step(self, step: WorkflowStep, arguments: dict[str, Any], *, confirmed: bool = False) -> StepOutcome:
        attempts = 0
        started = time.perf_counter()
        outcome: dict[str, Any] = {}
        invoke = self.confirm if confirmed and self.confirm is not None else self.invoke
        while True:
            attempts += 1
            outcome = _with_timeout(lambda: invoke(step.tool, arguments), step.timeout_seconds)
            if outcome.get("status") in {"success", "pending_confirmation"}:
                break
            if step.on_failure != "retry" or attempts > step.retries:
                break
            self.sleep(RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)])
        elapsed = (time.perf_counter() - started) * 1000
        return StepOutcome(
            step_id=step.id,
            tool=step.tool,
            status=str(outcome.get("status") or "failed"),
            message=str(outcome.get("message") or ""),
            error=outcome.get("error"),
            attempts=attempts,
            elapsed_ms=round(elapsed, 1),
            arguments=arguments,
            results=[item for item in (outcome.get("results") or []) if isinstance(item, dict)],
        )

    def _preview_step(self, step: WorkflowStep, arguments: dict[str, Any]) -> StepOutcome:
        if self.preview is None:
            return StepOutcome(step_id=step.id, tool=step.tool, status="preview", message="No preview available for this tool", arguments=arguments)
        try:
            outcome = self.preview(step.tool, arguments)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return StepOutcome(step_id=step.id, tool=step.tool, status="failed", error=type(error).__name__, message=str(error), arguments=arguments)
        return StepOutcome(
            step_id=step.id,
            tool=step.tool,
            status=str(outcome.get("status") or "preview"),
            message=str(outcome.get("message") or ""),
            error=outcome.get("error"),
            arguments=arguments,
            preview=outcome.get("preview"),
        )

    @staticmethod
    def _context_entry(outcome: StepOutcome) -> dict[str, Any]:
        return {
            "status": outcome.status,
            "message": outcome.message,
            "error": outcome.error,
            "results": list(outcome.results),
            "first": (outcome.results[0].get("data") if outcome.results else None),
        }


__all__ = ["Invoker", "Previewer", "RETRY_BACKOFF_SECONDS", "TIMED_OUT", "WorkflowRunner"]
