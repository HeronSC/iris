# File: core/assistant/why_command.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.audit.stream import AuditCategory
from core.observability.logging_setup import read_log_entries


def describe_activity(activity: dict[str, Any] | None) -> str:
    if not activity:
        return ""
    parts: list[str] = []
    steps = [item for item in (activity.get("actions") or []) + (activity.get("tools") or []) if item.get("name")]
    if steps:
        marks = []
        for item in steps:
            status = str(item.get("status") or "")
            mark = "ok" if status in {"success", "ok", "completed"} else ("waiting" if "pending" in status else status or "?")
            target = f" {item['target']}" if item.get("target") else ""
            marks.append(f"{item['name']}{target} ({mark})")
        parts.append("Tools: " + ", ".join(marks))
    calls = activity.get("model_calls") or []
    if calls:
        tokens = sum(int(item.get("prompt_tokens") or 0) + int(item.get("completion_tokens") or 0) for item in calls)
        wall = sum(float(item.get("wall_ms") or 0.0) for item in calls)
        models = []
        for item in calls:
            label = str(item.get("model") or "?")
            if label not in models:
                models.append(label)
        fallback = " (fallback)" if any(item.get("fallback") for item in calls) else ""
        parts.append(f"Model: {', '.join(models)}{fallback}, {len(calls)} call{'s' if len(calls) != 1 else ''}, {tokens} tokens, {wall / 1000:.1f} s")
    denied = [item for item in activity.get("permissions") or [] if str(item.get("status") or "").lower() in {"denied", "blocked"}]
    if denied:
        parts.append("Blocked: " + ", ".join(str(item.get("name")) for item in denied))
    elapsed = activity.get("elapsed_ms")
    if elapsed:
        parts.append(f"Total {float(elapsed) / 1000:.1f} s")
    return " · ".join(parts)


class WhyCommandHandler:
    def __init__(
        self,
        *,
        log_file: str | Path | None,
        trace_file: str | Path | None,
        metrics: Any | None,
        action_audit: Any | None,
        last_request_id: Any,
        audit_stream: Any | None = None,
        output: OutputSink | None = None,
    ) -> None:
        self.log_file = Path(log_file) if log_file else None
        self.trace_file = Path(trace_file) if trace_file else None
        self.metrics = metrics
        self.action_audit = action_audit
        self.audit_stream = audit_stream
        self.last_request_id = last_request_id
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        lowered = text.lower()
        if lowered != "/why" and not lowered.startswith("/why "):
            return False
        argument = text.split(maxsplit=1)[1].strip() if " " in text else ""
        request_id = argument or self._resolve_last()
        if not request_id:
            emit_output(self.output, "No request to explain yet. Ask something first, or give a request id: /why <id>")
            return True
        emit_output(self.output, self.explain(request_id))
        return True

    def _resolve_last(self) -> str | None:
        value = self.last_request_id() if callable(self.last_request_id) else self.last_request_id
        return str(value) if value else None


    def explain(self, request_id: str) -> str:
        turn = self._turn_entry(request_id)
        trace = self._trace_entry(request_id)
        model_calls = self._model_calls(request_id)
        actions = self._actions(request_id)
        tool_events = self._tools(request_id)
        permission_events = self._permissions(request_id)
        log_lines = self._log_lines(request_id)

        if turn is None and trace is None and not model_calls and not actions and not tool_events and not log_lines:
            return f"Nothing recorded for request {request_id}."

        lines = [f"Request {request_id}"]
        if turn is not None:
            lines.append(f"You asked: {turn.get('user_message', '')}")
            lines.append(
                f"Route: {turn.get('route', '?')} -> {turn.get('status', '?')} in {float(turn.get('elapsed_ms', 0)):.0f} ms"
            )
        if trace is not None:
            lines.extend(self._describe_trace(trace))
        if model_calls:
            lines.append("Model calls:")
            for row in model_calls:
                called = f", called {', '.join(row['tool_calls'])}" if row.get("tool_calls") else ""
                note = "" if row.get("outcome") == "ok" else f" [{row.get('outcome')}: {row.get('error')}]"
                fallback = " (fallback)" if row.get("fallback") else ""
                lines.append(
                    f"- {row.get('task') or 'default'} -> {row.get('model')}{fallback}: "
                    f"{row.get('prompt_tokens', 0)}+{row.get('completion_tokens', 0)} tokens, "
                    f"{float(row.get('wall_ms', 0)):.0f} ms{called}{note}"
                )
        if actions:
            lines.append("Actions:")
            for row in actions:
                tool = row.get("tool") or row.get("action")
                via = f" (via {tool})" if tool and tool != row.get("action") else ""
                target = f" on {row['resolved_target']}" if row.get("resolved_target") else ""
                lines.append(f"- {row.get('action')}{via}{target}: {row.get('status')} — {str(row.get('message', ''))[:120]}")
        if tool_events:
            lines.append("Tools:")
            for event in tool_events:
                kind = f" ({event.subject})" if event.subject else ""
                note = f" — {event.message}" if event.message else ""
                lines.append(f"- {event.event}{kind}: {event.status}{note}")
        if permission_events:
            lines.append("Permission:")
            for event in permission_events:
                target = f" on {event.target}" if event.target else ""
                reason = f" — {event.message}" if event.message else ""
                lines.append(f"- {event.event}{target}: {event.status}{reason}")
        warnings = [entry for entry in log_lines if str(entry.get("level", "")).lower() in {"warning", "error", "critical"}]
        if warnings:
            lines.append("Warnings:")
            for entry in warnings:
                lines.append(f"- {entry.get('logger', '')}: {entry.get('event', '')}")
        return "\n".join(lines)

    def summary(self, request_id: str | None) -> dict[str, Any]:
        if not request_id:
            return {}
        turn = self._turn_entry(request_id)
        model_calls = [
            {
                "task": row.get("task") or "default",
                "model": row.get("model"),
                "prompt_tokens": int(row.get("prompt_tokens") or 0),
                "completion_tokens": int(row.get("completion_tokens") or 0),
                "wall_ms": float(row.get("wall_ms") or 0.0),
                "outcome": row.get("outcome"),
                "fallback": bool(row.get("fallback")),
            }
            for row in self._model_calls(request_id)
        ]
        actions = [
            {"name": row.get("tool") or row.get("action"), "status": row.get("status"), "target": row.get("resolved_target"), "error": row.get("error")}
            for row in self._actions(request_id)
        ]
        tools = [{"name": event.event, "status": event.status, "kind": event.subject, "error": event.error} for event in self._tools(request_id)]
        permissions = [{"name": event.event, "status": event.status, "target": event.target, "reason": event.message} for event in self._permissions(request_id)]
        payload: dict[str, Any] = {"request_id": request_id, "model_calls": model_calls, "actions": actions, "tools": tools, "permissions": permissions}
        if turn is not None:
            payload["route"] = turn.get("route")
            payload["elapsed_ms"] = float(turn.get("elapsed_ms") or 0.0)
        return payload

    def _describe_trace(self, trace: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        parser = trace.get("deterministic_parser_result") or {}
        if isinstance(parser, dict) and parser.get("intent"):
            lines.append(f"Parsed intent: {parser.get('intent')}")
        orchestration = trace.get("orchestration")
        if isinstance(orchestration, dict) and orchestration:
            decision = orchestration.get("decision") or orchestration.get("decision_type")
            if decision:
                lines.append(f"Orchestrator decision: {decision}")
        tool = trace.get("selected_tool")
        if tool:
            arguments = trace.get("tool_arguments") or {}
            result = trace.get("tool_result") or {}
            lines.append(f"Capability: {tool} {json.dumps(arguments, ensure_ascii=False)} -> {result.get('status', '?')}")
        memory = trace.get("persistent_memory")
        if isinstance(memory, dict) and memory:
            retrieved = memory.get("retrieved_topics") or memory.get("topics") or memory.get("returned")
            if retrieved is not None:
                lines.append(f"Memory used: {retrieved}")
        response = str(trace.get("final_response") or "").strip()
        if response:
            lines.append(f"Answer: {response[:160]}{'…' if len(response) > 160 else ''}")
        return lines


    def _turn_entry(self, request_id: str) -> dict[str, Any] | None:
        for entry in self._log_lines(request_id):
            if entry.get("event") == "turn":
                return entry
        return None

    def _log_lines(self, request_id: str) -> list[dict[str, Any]]:
        if self.log_file is None:
            return []
        return read_log_entries(self.log_file, request_id=request_id, limit=100)

    def _trace_entry(self, request_id: str) -> dict[str, Any] | None:
        if self.trace_file is None or not self.trace_file.exists():
            return None
        try:
            lines = self.trace_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines[-500:]):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("request_id") == request_id:
                return entry
        return None

    def _model_calls(self, request_id: str) -> list[dict[str, Any]]:
        if self.metrics is None:
            return []
        try:
            rows = self.metrics.recent(limit=200)
        except Exception:
            return []
        matching = [row for row in rows if row.get("request_id") == request_id]
        matching.reverse()
        return matching

    def _tools(self, request_id: str) -> list[Any]:
        return self._stream_events(AuditCategory.TOOL, request_id)

    def _permissions(self, request_id: str) -> list[Any]:
        return self._stream_events(AuditCategory.PERMISSION, request_id)

    def _stream_events(self, category: AuditCategory, request_id: str) -> list[Any]:
        if self.audit_stream is None:
            return []
        try:
            return self.audit_stream.read(category=category, request_id=request_id, limit=50)
        except Exception:
            return []

    def _actions(self, request_id: str) -> list[dict[str, Any]]:
        if self.action_audit is None:
            return []
        try:
            rows = self.action_audit.read_recent(limit=200)
        except Exception:
            return []
        return [row for row in rows if row.get("request_id") == request_id]
