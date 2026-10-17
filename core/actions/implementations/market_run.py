# File: core/actions/implementations/market_run.py

from __future__ import annotations

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.market.research import MarketResearch
from core.tools.models import PermissionLevel, ToolDefinition


class MarketRunAction:
    name = "market_run"
    definition = ToolDefinition(
        name="market_run",
        description="Run the market research tool and return the saved top 10. Iris does not trade.",
        permission=PermissionLevel.READ,
        keywords=("market", "stocks", "top 10", "research stocks"),
    )

    def __init__(self, research: MarketResearch) -> None:
        self.research = research

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        _ = context
        return ValidationResult(ok=True, resolved_arguments=dict(request.arguments))

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        _ = context
        profile = str(request.arguments.get("profile") or "") or None
        report = self.research.run(profile)
        if not report.ok:
            return ActionResult(status="failed", message=report.message, action=self.name, error="market_failed")
        return ActionResult(status="success", message=report.chat, action=self.name, resolved_target=report.details)
