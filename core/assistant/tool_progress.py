# File: core/assistant/tool_progress.py

from __future__ import annotations

from typing import Any

RUNNING_PREFIX = "Running "
FINISHED_PREFIX = "Finished "


def _target(arguments: dict[str, Any] | None) -> str:
    if not arguments:
        return ""
    for key in ("query", "pattern", "host", "name", "path", "range", "text", "url", "topic"):
        value = arguments.get(key)
        if value:
            text = str(value).strip()
            return text if len(text) <= 60 else text[:57] + "..."
    return ""


def describe_tool_event(event: dict[str, Any]) -> str:
    name = str(event.get("name") or "a tool")
    phase = str(event.get("phase") or "")
    if phase == "start":
        target = _target(event.get("arguments"))
        return f"{RUNNING_PREFIX}{name}" + (f" on {target}" if target else "") + "…"
    status = str(event.get("status") or "done")
    elapsed = event.get("ms")
    timing = f" in {float(elapsed) / 1000:.1f} s" if isinstance(elapsed, (int, float)) else ""
    outcome = {"success": "ok", "pending_confirmation": "waiting for approval", "failed": "failed", "rejected": "rejected"}.get(status, status)
    line = f"{FINISHED_PREFIX}{name}{timing}: {outcome}"
    error = event.get("error")
    if error and status not in {"success", "pending_confirmation"}:
        line += f" ({str(error)[:80]})"
    return line


def is_tool_progress(text: str) -> bool:
    return text.startswith(RUNNING_PREFIX) or text.startswith(FINISHED_PREFIX)


__all__ = ["FINISHED_PREFIX", "RUNNING_PREFIX", "describe_tool_event", "is_tool_progress"]
