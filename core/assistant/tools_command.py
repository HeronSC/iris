# File: core/assistant/tools_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.tools.registry import ToolRegistry


class ToolsCommandHandler:

    def __init__(self, registry: ToolRegistry, mcp_manager: Any | None = None, output: OutputSink | None = None) -> None:
        self.registry = registry
        self.mcp_manager = mcp_manager
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        if text.lower() != "/tools" and not text.lower().startswith("/tools "):
            return False
        parts = text.split(maxsplit=2)
        subcommand = parts[1].lower() if len(parts) > 1 else "list"
        argument = parts[2].strip() if len(parts) > 2 else ""

        if subcommand == "list":
            self._list()
            return True
        if subcommand == "mcp":
            self._mcp_status()
            return True
        if subcommand in {"disable", "enable"}:
            if not argument:
                emit_output(self.output, f"Usage: /tools {subcommand} <tool name>")
                return True
            if argument not in self.registry:
                emit_output(self.output, f"Unknown tool: {argument}")
                return True
            if subcommand == "disable":
                self.registry.disable(argument)
                emit_output(self.output, f"Disabled {argument}. It stays registered and can be re-enabled with /tools enable {argument}.")
            else:
                self.registry.enable(argument)
                emit_output(self.output, f"Enabled {argument}.")
            return True

        emit_output(self.output, "Usage: /tools [list|mcp|disable <name>|enable <name>]")
        return True

    def _list(self) -> None:
        rows = self.registry.describe()
        if not rows:
            emit_output(self.output, "No tools are registered.")
            return
        lines = [f"{len(rows)} tools registered ({sum(1 for row in rows if row['exposed'] and row['enabled'])} offered to the model):"]
        for row in rows:
            flags = [row["kind"], row["permission"]]
            if row["requires_confirmation"]:
                flags.append("confirm")
            if not row["exposed"]:
                flags.append("hidden")
            if not row["enabled"]:
                flags.append("DISABLED")
            if row["action"]:
                flags.append(f"-> {row['action']}")
            if row["source"] != "native":
                flags.append(row["source"])
            lines.append(f"- {row['name']} [{', '.join(flags)}] {row['description']}")
        emit_output(self.output, "\n".join(lines))

    def _mcp_status(self) -> None:
        if self.mcp_manager is None:
            emit_output(self.output, "No MCP servers are configured.")
            return
        rows = self.mcp_manager.status()
        if not rows:
            emit_output(self.output, "No MCP servers are configured.")
            return
        lines: list[str] = []
        for row in rows:
            if not row["enabled"]:
                state = "disabled in config"
            elif row["running"]:
                state = f"running{' v' + row['version'] if row['version'] else ''}, {len(row['tools'])} tools"
            elif row["error"]:
                state = f"unavailable: {row['error']}"
            else:
                state = "starting"
            lines.append(f"- {row['server']}: {state}")
            if row["tools"]:
                lines.append("  " + ", ".join(row["tools"]))
        emit_output(self.output, "\n".join(lines))
