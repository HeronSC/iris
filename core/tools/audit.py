# File: core/tools/audit.py

"""Every tool invocation, in the one audit stream (2.3, 2.8).

Actions and MCP calls were already audited: both run through
``core/actions/executor.py``, which writes a line per call. Command tools and
knowledge providers were not -- the model could ask for the weather or run a
slash command and nothing recorded it -- so "why did you do that?" had a hole
in it exactly where the tool layer is widest.

This closes it at the dispatch point rather than in each handler, which is
what makes it hold for tools nobody has written yet. Actions are skipped here
on purpose: the executor's own entry is the better record (it knows the
resolved target and the confirmation), and two lines for one call would make
counting invocations wrong.
"""

from __future__ import annotations

from typing import Any

from core.audit.stream import AuditCategory, AuditEvent, AuditStream
from core.tools.models import ToolKind

#: Kinds whose invocations are recorded elsewhere, by something that knows more.
AUDITED_ELSEWHERE = frozenset({ToolKind.ACTION})


class ToolAuditor:
    def __init__(self, stream: AuditStream, registry: Any | None = None) -> None:
        self.stream = stream
        self.registry = registry

    def record(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        result: dict[str, Any] | None,
        *,
        source: str = "agent",
        kind: ToolKind | None = None,
    ) -> AuditEvent | None:
        definition = self.registry.get(name) if self.registry is not None else None
        resolved_kind = kind or (definition.kind if definition is not None else None)
        if resolved_kind in AUDITED_ELSEWHERE:
            return None
        outcome = dict(result or {})
        data: dict[str, Any] = {"arguments": dict(arguments or {})}
        if definition is not None:
            data["tool_version"] = definition.version
            data["permission"] = definition.permission.value
        return self.stream.write(
            AuditEvent(
                category=AuditCategory.TOOL,
                event=name,
                subject=resolved_kind.value if resolved_kind is not None else None,
                source=source,
                status=str(outcome.get("status") or "unknown"),
                message=_first_line(outcome.get("message")),
                error=_text(outcome.get("error")),
                data=data,
            )
        )


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _first_line(value: Any) -> str | None:
    """A tool's answer can be a page of prose; the audit wants the gist."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    first = text.splitlines()[0]
    return first if len(first) <= 200 else f"{first[:197]}..."


__all__ = ["ToolAuditor", "AUDITED_ELSEWHERE"]
