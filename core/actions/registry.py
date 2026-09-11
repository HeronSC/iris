# File: core/actions/registry.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.tools.models import ToolArgumentError, ToolDefinition, ToolKind
from core.tools.registry import ToolRegistry


class Action(Protocol):
    @property
    def name(self) -> str:
        ...

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        ...

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        ...


@dataclass(frozen=True)
class ResolvedAction:

    definition: ToolDefinition
    action: Action
    request: ActionRequest
    error: str | None = None


class ActionRegistry:

    def __init__(self, tools: ToolRegistry | None = None) -> None:
        self.tools = tools if tools is not None else ToolRegistry()

    def register(self, action: Action, replace: bool = False) -> None:
        definition = getattr(action, "definition", None)
        if definition is None:
            definition = ToolDefinition(name=action.name, description=action.name, expose_to_model=False)
        if definition.name != action.name:
            raise ValueError(f"Action {action.name} declares a definition named {definition.name}")
        self.tools.register(definition.with_handler(action), replace=replace)
        facets: Iterable[ToolDefinition] = getattr(action, "facets", ()) or ()
        for facet in facets:
            bound = ToolDefinition(
                name=facet.name,
                description=facet.description,
                arguments=facet.arguments,
                parameters=facet.parameters,
                permission=facet.permission,
                requires_confirmation=facet.requires_confirmation,
                kind=ToolKind.ACTION,
                action=action.name,
                bind=dict(facet.bind),
                version=facet.version,
                expose_to_model=facet.expose_to_model,
                source=facet.source,
                handler=action,
            )
            self.tools.register(bound, replace=replace)

    def get(self, name: str) -> Action | None:
        definition = self.tools.get(name)
        if definition is None or definition.kind != ToolKind.ACTION:
            return None
        handler = definition.handler
        return handler if handler is not None else None

    def definition(self, name: str) -> ToolDefinition | None:
        definition = self.tools.get(name)
        if definition is None or definition.kind != ToolKind.ACTION:
            return None
        return definition

    def names(self) -> list[str]:
        return [item.name for item in self.tools.definitions(kind=ToolKind.ACTION) if not item.is_facet]

    def is_enabled(self, name: str) -> bool:
        return self.tools.is_enabled(name)

    def resolve(self, request: ActionRequest) -> ResolvedAction | None:
        definition = self.definition(request.action)
        if definition is None:
            return None
        action = definition.handler
        if action is None:
            return None
        if not definition.is_facet:
            return ResolvedAction(definition=definition, action=action, request=request)
        try:
            arguments = definition.validate_arguments(request.arguments)
        except ToolArgumentError as error:
            return ResolvedAction(definition=definition, action=action, request=request, error=str(error))
        flattened = ActionRequest(
            action=definition.target_action,
            arguments=arguments,
            source=request.source,
            reason=request.reason,
            follow_up=request.follow_up,
            workflow_goal=request.workflow_goal,
            workflow_parameters=request.workflow_parameters,
        )
        return ResolvedAction(definition=definition, action=action, request=flattened)
