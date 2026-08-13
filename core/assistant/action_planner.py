from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.actions.executor import ActionExecutionContext
from core.actions.models import ActionRequest


@dataclass(frozen=True)
class WorkflowPlan:
    goal: str
    parameters: dict[str, str]
    request: ActionRequest


class ActionPlanner:
    def __init__(self, context: ActionExecutionContext) -> None:
        self.context = context

    def plan_make_directory_searchable(self, root: str) -> WorkflowPlan:
        root_path = Path(root).expanduser()
        try:
            resolved = root_path.resolve()
        except OSError:
            resolved = root_path

        goal = "make_directory_searchable"
        parameters = {"root": str(resolved)}

        if self._is_configured_document_root(resolved):
            return WorkflowPlan(
                goal=goal,
                parameters=parameters,
                request=ActionRequest(
                    action="scan_document_root",
                    arguments={"root": str(resolved)},
                    source="intent",
                    reason=f"Scan configured document root '{resolved}'",
                    workflow_goal=goal,
                    workflow_parameters=parameters,
                ),
            )

        return WorkflowPlan(
            goal=goal,
            parameters=parameters,
            request=ActionRequest(
                action="add_document_root",
                arguments={"root": str(resolved)},
                source="intent",
                reason=f"Add document root before scanning '{resolved}'",
                workflow_goal=goal,
                workflow_parameters=parameters,
                follow_up=ActionRequest(
                    action="scan_document_root",
                    arguments={"root": str(resolved)},
                    source="intent-follow-up",
                    reason=f"Scan document root '{resolved}' after adding it",
                    workflow_goal=goal,
                    workflow_parameters=parameters,
                ),
            ),
        )

    def _is_configured_document_root(self, root: Path) -> bool:
        root_value = str(root).lower()
        for configured_root in self.context.allowed_roots:
            try:
                configured_resolved = configured_root.resolve()
            except OSError:
                configured_resolved = configured_root
            if str(configured_resolved).lower() == root_value:
                return True
        return False