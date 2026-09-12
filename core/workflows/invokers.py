# File: core/workflows/invokers.py

from __future__ import annotations

from typing import Any

from core.actions.models import ActionRequest
from core.results.models import to_json_list
from core.workflows.runner import Invoker, Previewer


def executor_invoker(executor: Any, *, source: str = "workflow") -> Invoker:
    def invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        has_pending = getattr(executor, "has_pending_confirmation", None)
        if callable(has_pending) and has_pending():
            return {"status": "failed", "error": "confirmation_already_pending", "message": "Another action is awaiting confirmation."}
        result = executor.execute(ActionRequest(action=name, arguments=dict(arguments), source=source, reason=f"Workflow step {name}"))
        return {"status": result.status, "error": result.error, "message": result.message, "results": to_json_list(result.results)}

    return invoke


def executor_confirmer(executor: Any) -> Invoker:
    def invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        outcome = executor_invoker(executor)(name, arguments)
        if outcome.get("status") == "pending_confirmation":
            confirmed = executor.confirm_pending()
            return {"status": confirmed.status, "error": confirmed.error, "message": confirmed.message, "results": to_json_list(confirmed.results)}
        return outcome

    return invoke


def executor_previewer(executor: Any, *, source: str = "workflow") -> Previewer:
    def preview(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return executor.preview(ActionRequest(action=name, arguments=dict(arguments), source=source, reason=f"Workflow step {name}"))

    return preview


def agent_invoker(coordinator: Any, executor: Any | None = None) -> Invoker:
    def invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        agent = getattr(coordinator, "agent", None)
        run_tool = getattr(agent, "_dispatch", None)
        if callable(run_tool):
            outcome = run_tool(name, dict(arguments), "workflow")
            return dict(outcome) if isinstance(outcome, dict) else {"status": "failed", "error": "no_outcome", "message": "The tool produced nothing"}
        if executor is not None:
            return executor_invoker(executor)(name, arguments)
        return {"status": "failed", "error": "no_invoker", "message": f"No way to run {name} here"}

    return invoke


__all__ = ["agent_invoker", "executor_confirmer", "executor_invoker", "executor_previewer"]
