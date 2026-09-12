# File: core/tests/test_tool_contract.py

"""Tool contract (2.3): a per-tool timeout, cancellation, and an advertised cost."""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from typing import Any

from core.actions.executor import ActionExecutionContext, ActionExecutor, SystemAdapter
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.tools.models import PermissionLevel, ToolDefinition
from core.tools.registry import ToolRegistry


class _Audit:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def log(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append((args, kwargs))

    def record(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append((args, kwargs))


class SlowAction:
    name = "slow"
    definition = ToolDefinition(name="slow", description="Sleeps.", permission=PermissionLevel.READ, timeout_seconds=0.3, cost="a while")

    def __init__(self) -> None:
        self.finished = threading.Event()

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        return ValidationResult(ok=True)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        time.sleep(float(request.arguments.get("seconds", 1.0)))
        self.finished.set()
        return ActionResult(status="success", message="slept", action=self.name)


class QuickAction:
    name = "quick"
    definition = ToolDefinition(name="quick", description="Returns at once.", permission=PermissionLevel.READ)

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        return ValidationResult(ok=True)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        return ActionResult(status="success", message="done", action=self.name)


class BrokenAction:
    name = "broken"
    definition = ToolDefinition(name="broken", description="Raises.", permission=PermissionLevel.READ)

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        return ValidationResult(ok=True)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        raise RuntimeError("boom")


def _executor(*actions: Any, default_timeout: float = 120.0) -> ActionExecutor:
    registry = ActionRegistry(ToolRegistry())
    for action in actions:
        registry.register(action)
    context = ActionExecutionContext(catalog=None, allowed_roots=[Path(".")], applications={}, app_alias_map={}, web_shortcuts={}, system=SystemAdapter())
    return ActionExecutor(registry, ActionPolicy(), _Audit(), context, default_timeout_seconds=default_timeout)


class ContractTests(unittest.TestCase):
    def test_a_tool_that_overruns_its_declared_timeout_is_abandoned(self) -> None:
        slow = SlowAction()
        executor = _executor(slow)
        started = time.perf_counter()
        result = executor.execute(ActionRequest(action="slow", arguments={"seconds": 2.0}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "timeout")
        self.assertIn("did not finish within 0 s", result.message)
        self.assertLess(time.perf_counter() - started, 1.5)
        self.assertFalse(slow.finished.is_set())

    def test_a_quick_tool_and_a_broken_tool_keep_their_behaviour(self) -> None:
        executor = _executor(QuickAction(), BrokenAction())
        self.assertEqual(executor.execute(ActionRequest(action="quick", arguments={})).status, "success")
        broken = executor.execute(ActionRequest(action="broken", arguments={}))
        self.assertEqual(broken.status, "failed")
        self.assertIn("boom", broken.message)

    def test_a_cancel_event_stops_waiting_for_a_running_tool(self) -> None:
        slow = SlowAction()
        executor = _executor(slow, default_timeout=10.0)
        cancel = threading.Event()
        executor.cancel_event = cancel
        threading.Timer(0.25, cancel.set).start()
        started = time.perf_counter()
        result = executor.execute(ActionRequest(action="slow", arguments={"seconds": 3.0}))
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.error, "cancelled")
        self.assertLess(time.perf_counter() - started, 1.5)

    def test_cost_and_timeout_are_part_of_the_definition(self) -> None:
        self.assertEqual(SlowAction.definition.cost, "a while")
        self.assertEqual(SlowAction.definition.timeout_seconds, 0.3)
        self.assertIsNone(QuickAction.definition.timeout_seconds)
        executor = _executor(QuickAction(), default_timeout=7.0)
        self.assertEqual(executor._timeout_for("quick"), 7.0)
        self.assertEqual(_executor(SlowAction())._timeout_for("slow"), 0.3)


if __name__ == "__main__":
    unittest.main()
