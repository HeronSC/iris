# File: core/actions/policy.py

from __future__ import annotations

from core.actions.models import ActionRequest
from core.tools.models import ToolDefinition


class ActionPolicy:

    def requires_confirmation(self, request: ActionRequest, definition: ToolDefinition | None = None) -> bool:
        if definition is not None:
            return bool(definition.requires_confirmation)
        return False
