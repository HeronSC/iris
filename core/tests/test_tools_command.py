# File: core/tests/test_tools_command.py

from __future__ import annotations

import unittest
from typing import Any

from core.actions.implementations.launch_application import LaunchApplicationAction
from core.actions.implementations.update_config import UpdateConfigAction
from core.actions.registry import ActionRegistry
from core.assistant.tools_command import ToolsCommandHandler


class FakeMcpManager:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def status(self) -> list[dict[str, Any]]:
        return self.rows


class ToolsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ActionRegistry()
        self.registry.register(LaunchApplicationAction())
        self.registry.register(UpdateConfigAction())
        self.output: list[str] = []
        self.handler = ToolsCommandHandler(
            self.registry.tools,
            FakeMcpManager([{"server": "git", "enabled": True, "running": True, "tools": ["git_status"], "error": None, "version": "1.30.0"}]),
            output=lambda text, role=None: self.output.append(text),
        )

    def test_ignores_other_commands(self) -> None:
        self.assertFalse(self.handler.handle("/toolsmith", {}))
        self.assertFalse(self.handler.handle("hello", {}))

    def test_lists_tools_with_flags(self) -> None:
        self.assertTrue(self.handler.handle("/tools", {}))
        listing = self.output[-1]
        self.assertIn("launch_application [action, execute]", listing)
        self.assertIn("config_set_value [action, write, confirm, -> update_config]", listing)
        self.assertIn("update_config [action, write, confirm, hidden]", listing)

    def test_disable_and_enable_round_trip(self) -> None:
        self.assertTrue(self.handler.handle("/tools disable launch_application", {}))
        self.assertFalse(self.registry.tools.is_enabled("launch_application"))
        self.assertNotIn("launch_application", {spec.name for spec in self.registry.tools.model_tools()})
        self.handler.handle("/tools", {})
        self.assertIn("launch_application [action, execute, DISABLED]", self.output[-1])

        self.assertTrue(self.handler.handle("/tools enable launch_application", {}))
        self.assertTrue(self.registry.tools.is_enabled("launch_application"))

        self.assertTrue(self.handler.handle("/tools disable nope", {}))
        self.assertIn("Unknown tool", self.output[-1])
        self.assertTrue(self.handler.handle("/tools disable", {}))
        self.assertIn("Usage", self.output[-1])

    def test_mcp_status(self) -> None:
        self.assertTrue(self.handler.handle("/tools mcp", {}))
        self.assertIn("git: running v1.30.0, 1 tools", self.output[-1])
        self.assertIn("git_status", self.output[-1])

        self.assertTrue(ToolsCommandHandler(self.registry.tools, None, output=lambda t, r=None: self.output.append(t)).handle("/tools mcp", {}))
        self.assertIn("No MCP servers", self.output[-1])


if __name__ == "__main__":
    unittest.main()
