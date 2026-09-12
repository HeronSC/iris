# File: core/llm/budget.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_BUDGET_MS: dict[str, float] = {
    "default": 8000.0,
    "chat": 8000.0,
    "code": 20000.0,
    "summary": 12000.0,
    "intent": 2500.0,
    "decision": 6000.0,
    "embedding": 2000.0,
}

MIN_CALLS = 3


@dataclass(frozen=True)
class LatencyBudget:
    budgets_ms: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BUDGET_MS))

    @classmethod
    def from_config(cls, config: Any) -> "LatencyBudget":
        raw = config.get("latency_budget_ms") if isinstance(config, dict) else None
        budgets = dict(DEFAULT_BUDGET_MS)
        if isinstance(raw, dict):
            for task, value in raw.items():
                try:
                    budgets[str(task or "default")] = float(value)
                except (TypeError, ValueError):
                    continue
        return cls(budgets_ms=budgets)

    def for_task(self, task: str | None) -> float:
        return self.budgets_ms.get(str(task or "default"), self.budgets_ms.get("default", DEFAULT_BUDGET_MS["default"]))

    def check(self, rows: list[dict[str, Any]], *, min_calls: int = MIN_CALLS) -> list[str]:
        warnings: list[str] = []
        for row in rows:
            calls = int(row.get("calls", 0) or 0)
            if calls < min_calls:
                continue
            task = str(row.get("task") or "default")
            average = float(row.get("avg_wall_ms", 0.0) or 0.0)
            budget = self.for_task(task)
            if average > budget:
                warnings.append(
                    f"{task or 'default'} on {row.get('model', '?')} averages {average / 1000:.1f} s over {calls} calls; "
                    f"its budget is {budget / 1000:.1f} s"
                )
        return warnings

    def describe(self) -> list[str]:
        return [f"{task}: {value / 1000:g} s" for task, value in sorted(self.budgets_ms.items())]


__all__ = ["DEFAULT_BUDGET_MS", "LatencyBudget", "MIN_CALLS"]
