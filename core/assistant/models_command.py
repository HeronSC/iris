# File: core/assistant/models_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output


class ModelsCommandHandler:

    def __init__(self, router: Any, metrics: Any | None = None, output: OutputSink | None = None) -> None:
        self.router = router
        self.metrics = metrics
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        if text.lower() != "/models" and not text.lower().startswith("/models "):
            return False
        parts = text.split()
        subcommand = parts[1].lower() if len(parts) > 1 else "routes"
        argument = parts[2] if len(parts) > 2 else ""

        if subcommand == "routes":
            self._routes()
            return True
        if subcommand == "usage":
            hours = self._number(argument)
            self._usage(hours)
            return True
        if subcommand == "recent":
            limit = int(self._number(argument) or 10)
            self._recent(limit)
            return True
        emit_output(self.output, "Usage: /models [routes|usage [hours]|recent [count]]")
        return True

    def _routes(self) -> None:
        status = self.router.status()
        lines = ["Model routes:"]
        for row in status["routes"]:
            mark = "" if row["pulled"] else "  (not pulled)"
            lines.append(f"- {row['task']}: {row['model']}{mark}")
        if status["fallbacks"]:
            lines.append("Fallbacks:")
            for model, chain in status["fallbacks"].items():
                lines.append(f"- {model} -> {', '.join(chain)}")
        lines.append(f"Planning tasks (need tool-calling): {', '.join(status['planning_tasks'])}")
        available = status["available"]
        lines.append(f"Pulled models: {', '.join(available) if available else 'unknown (Ollama did not answer)'}")
        warnings = self.router.check_routes()
        if warnings:
            lines.append("Warnings:")
            lines.extend(f"- {item}" for item in warnings)
        emit_output(self.output, "\n".join(lines))

    def _usage(self, hours: float | None) -> None:
        if self.metrics is None:
            emit_output(self.output, "Model metrics are not enabled.")
            return
        rows = self.metrics.summary(hours=hours)
        if not rows:
            emit_output(self.output, "No model calls recorded" + (f" in the last {hours:g} hours." if hours else "."))
            return
        header = "Model usage" + (f" (last {hours:g} hours)" if hours else "") + ":"
        lines = [header]
        total_calls = sum(row["calls"] for row in rows)
        total_prompt = sum(row["prompt_tokens"] for row in rows)
        total_completion = sum(row["completion_tokens"] for row in rows)
        for row in rows:
            extras = []
            if row["errors"]:
                extras.append(f"{row['errors']} errors")
            if row["fallbacks"]:
                extras.append(f"{row['fallbacks']} fallbacks")
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(
                f"- {row['task'] or 'default'} on {row['model']}: {row['calls']} calls, "
                f"{row['prompt_tokens']}+{row['completion_tokens']} tokens, "
                f"avg {row['avg_wall_ms']:.0f} ms, max {row['max_wall_ms']:.0f} ms{suffix}"
            )
        lines.append(f"Total: {total_calls} calls, {total_prompt}+{total_completion} tokens")
        emit_output(self.output, "\n".join(lines))

    def _recent(self, limit: int) -> None:
        if self.metrics is None:
            emit_output(self.output, "Model metrics are not enabled.")
            return
        rows = self.metrics.recent(limit=limit)
        if not rows:
            emit_output(self.output, "No model calls recorded.")
            return
        lines = [f"Last {len(rows)} model calls:"]
        for row in rows:
            tools = f" tools={','.join(row['tool_calls'])}" if row["tool_calls"] else ""
            note = f" {row['outcome']}" if row["outcome"] != "ok" else ""
            fallback = " (fallback)" if row["fallback"] else ""
            lines.append(
                f"- {row['created_at'][11:19]} {row['task'] or 'default'} -> {row['model']}{fallback}: "
                f"{row['prompt_tokens']}+{row['completion_tokens']} tokens, {row['wall_ms']:.0f} ms{tools}{note}"
            )
        emit_output(self.output, "\n".join(lines))

    @staticmethod
    def _number(value: str) -> float | None:
        try:
            return float(value) if value else None
        except ValueError:
            return None
