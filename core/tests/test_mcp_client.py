# File: core/tests/test_mcp_client.py

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.actions.audit import ActionAuditLogger
from core.actions.executor import ActionExecutionContext, ActionExecutor, SystemAdapter
from core.actions.models import ActionRequest
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.tools.mcp_client import (
    McpCallResult,
    McpManager,
    McpServerConfig,
    McpServerError,
    McpTool,
    McpToolAction,
    exposed_schema,
    load_server_configs,
    permission_for,
    resolve_command,
)
from core.tools.models import PermissionLevel


def _tool(name: str, read_only: bool | None, destructive: bool | None = None, required: tuple[str, ...] = ("repo_path",)) -> McpTool:
    annotations: dict[str, Any] = {}
    if read_only is not None:
        annotations["read_only_hint"] = read_only
    if destructive is not None:
        annotations["destructive_hint"] = destructive
    properties = {key: {"type": "string"} for key in required}
    properties["context_lines"] = {"type": "integer", "default": 3}
    return McpTool(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object", "properties": properties, "required": list(required)},
        annotations=annotations,
    )


class FakeConnection:
    def __init__(self, config: McpServerConfig, running: bool = True) -> None:
        self.config = config
        self.running = running
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = McpCallResult(text="ok")

    def call(self, name: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> McpCallResult:
        self.calls.append((name, dict(arguments or {})))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ConfigTests(unittest.TestCase):
    def test_load_server_configs_reads_the_block(self) -> None:
        configs = load_server_configs(
            {
                "git": {"command": "uvx", "args": ["mcp-server-git"], "bind": {"repo_path": "E:\\repo"}, "confirm": ["git_add"]},
                "broken": {"args": []},
                "off": {"command": "x", "enabled": False},
                "junk": "not a dict",
            }
        )
        names = [config.name for config in configs]
        self.assertEqual(names, ["git", "off"])
        git = configs[0]
        self.assertEqual(git.command, "uvx")
        self.assertEqual(git.args, ("mcp-server-git",))
        self.assertEqual(git.bind, {"repo_path": "E:\\repo"})
        self.assertEqual(git.confirm, ("git_add",))
        self.assertTrue(git.enabled)
        self.assertFalse(configs[1].enabled)
        self.assertEqual(load_server_configs(None), [])

    def test_resolve_command_prefers_path_then_local_bin(self) -> None:
        python = shutil.which("python") or shutil.which("py")
        if python:
            self.assertTrue(os.path.isabs(resolve_command(Path(python).stem)))
        self.assertEqual(resolve_command("definitely-not-a-command-xyz"), "definitely-not-a-command-xyz")


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = McpServerConfig(name="git", command="uvx", confirm=("git_log",), no_confirm=("git_add",))

    def test_read_only_tools_run_without_confirmation(self) -> None:
        self.assertEqual(permission_for(_tool("git_status", True), self.config), (PermissionLevel.READ, False))

    def test_writes_confirm_and_destructive_is_execute(self) -> None:
        self.assertEqual(permission_for(_tool("git_commit", False), self.config), (PermissionLevel.WRITE, True))
        self.assertEqual(permission_for(_tool("git_reset", False, True), self.config), (PermissionLevel.EXECUTE, True))

    def test_unannotated_tools_confirm_by_default(self) -> None:
        self.assertEqual(permission_for(_tool("mystery", None), self.config), (PermissionLevel.WRITE, True))

    def test_config_overrides_annotations(self) -> None:
        self.assertEqual(permission_for(_tool("git_log", True), self.config)[1], True)
        self.assertEqual(permission_for(_tool("git_add", False), self.config)[1], False)


class SchemaTests(unittest.TestCase):
    def test_bound_arguments_are_hidden_from_the_model(self) -> None:
        tool = _tool("git_diff", True, required=("repo_path", "target"))
        schema = exposed_schema(tool.input_schema, {"repo_path": "E:\\repo"})
        self.assertNotIn("repo_path", schema["properties"])
        self.assertEqual(schema["required"], ["target"])
        self.assertIn("target", schema["properties"])
        # the original is untouched
        self.assertIn("repo_path", tool.input_schema["properties"])


class ActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = McpServerConfig(name="git", command="uvx", bind={"repo_path": "E:\\repo"})
        self.connection = FakeConnection(self.config)
        self.context = ActionExecutionContext(
            catalog=None,  # type: ignore[arg-type]
            allowed_roots=[],
            applications={},
            app_alias_map={},
            web_shortcuts={},
            system=SystemAdapter(),
        )

    def test_definition_reflects_annotations_and_bindings(self) -> None:
        action = McpToolAction(self.connection, _tool("git_status", True), "git_status")  # type: ignore[arg-type]
        self.assertEqual(action.definition.permission, PermissionLevel.READ)
        self.assertFalse(action.definition.requires_confirmation)
        self.assertEqual(action.definition.source, "mcp:git")
        self.assertNotIn("repo_path", action.definition.parameters_schema()["properties"])

    def test_validate_fills_bound_arguments_and_checks_schema(self) -> None:
        action = McpToolAction(self.connection, _tool("git_diff", True, required=("repo_path", "target")), "git_diff")  # type: ignore[arg-type]
        ok = action.validate(ActionRequest(action="git_diff", arguments={"target": "main"}), self.context)
        self.assertTrue(ok.ok)
        self.assertEqual(ok.resolved_arguments, {"target": "main", "repo_path": "E:\\repo"})
        self.assertIsNone(ok.confirmation_preview, "read-only tools do not need a preview")

        bad = action.validate(ActionRequest(action="git_diff", arguments={}), self.context)
        self.assertFalse(bad.ok)
        self.assertIn("target", bad.error or "")

        wrong_type = action.validate(ActionRequest(action="git_diff", arguments={"target": "main", "context_lines": "three"}), self.context)
        self.assertFalse(wrong_type.ok)

    def test_writes_carry_a_preview_and_the_executor_asks_first(self) -> None:
        registry = ActionRegistry()
        action = McpToolAction(self.connection, _tool("git_commit", False, required=("repo_path", "message")), "git_commit")  # type: ignore[arg-type]
        registry.register(action)
        with tempfile.TemporaryDirectory() as tmp:
            executor = ActionExecutor(registry=registry, policy=ActionPolicy(), audit=ActionAuditLogger(tmp), context=self.context)
            result = executor.execute(ActionRequest(action="git_commit", arguments={"message": "wip"}))
            self.assertEqual(result.status, "pending_confirmation")
            self.assertIn("git_commit", result.message)
            self.assertIn("wip", result.message)
            self.assertEqual(self.connection.calls, [])

            confirmed = executor.confirm_pending()
            self.assertEqual(confirmed.status, "success")
            self.assertEqual(self.connection.calls, [("git_commit", {"message": "wip", "repo_path": "E:\\repo"})])

    def test_errors_become_failed_results(self) -> None:
        action = McpToolAction(self.connection, _tool("git_status", True), "git_status")  # type: ignore[arg-type]
        self.connection.result = McpCallResult(text="fatal: not a git repository", is_error=True)
        result = action.execute(ActionRequest(action="git_status", arguments={"repo_path": "E:\\repo"}), self.context)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "mcp_tool_error")

        self.connection.result = McpServerError("boom")  # type: ignore[assignment]
        result = action.execute(ActionRequest(action="git_status", arguments={"repo_path": "E:\\repo"}), self.context)
        self.assertEqual(result.error, "mcp_call_failed")

    def test_validate_refuses_when_server_is_down(self) -> None:
        self.connection.running = False
        action = McpToolAction(self.connection, _tool("git_status", True), "git_status")  # type: ignore[arg-type]
        result = action.validate(ActionRequest(action="git_status", arguments={}), self.context)
        self.assertFalse(result.ok)
        self.assertIn("not running", result.error or "")


class ManagerTests(unittest.TestCase):
    def test_unavailable_server_is_reported_not_raised(self) -> None:
        registry = ActionRegistry()
        events: list[str] = []
        manager = McpManager(
            [McpServerConfig(name="ghost", command="definitely-not-a-command-xyz", ready_timeout_seconds=5)],
            registry,
            on_event=events.append,
        )
        manager.start(background=False)
        status = manager.status()[0]
        self.assertFalse(status["running"])
        self.assertIsNotNone(status["error"])
        self.assertEqual(registry.names(), [])
        self.assertEqual(len(events), 1)
        manager.stop()


@unittest.skipUnless(os.path.isabs(resolve_command("uvx")), "uvx is not installed")
class LiveGitServerTests(unittest.TestCase):
    """Talks to the real mcp-server-git; skipped where uvx is missing."""

    def test_git_status_runs_through_the_executor(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        registry = ActionRegistry()
        manager = McpManager(
            [McpServerConfig(name="git", command="uvx", args=("mcp-server-git",), bind={"repo_path": str(repo)}, ready_timeout_seconds=90)],
            registry,
        )
        manager.start(background=False)
        try:
            status = manager.status()[0]
            self.assertTrue(status["running"], status["error"])
            self.assertIn("git_status", status["tools"])
            exposed = {spec.name for spec in registry.tools.model_tools()}
            self.assertIn("git_status", exposed)
            with tempfile.TemporaryDirectory() as tmp:
                executor = ActionExecutor(
                    registry=registry,
                    policy=ActionPolicy(),
                    audit=ActionAuditLogger(tmp),
                    context=ActionExecutionContext(
                        catalog=None,  # type: ignore[arg-type]
                        allowed_roots=[],
                        applications={},
                        app_alias_map={},
                        web_shortcuts={},
                        system=SystemAdapter(),
                    ),
                )
                result = executor.execute(ActionRequest(action="git_status", arguments={}))
                self.assertEqual(result.status, "success", result.message)
                self.assertIn("branch", result.message.lower())
                pending = executor.execute(ActionRequest(action="git_commit", arguments={"message": "never sent"}))
                self.assertEqual(pending.status, "pending_confirmation")
                self.assertEqual(executor.cancel_pending().status, "cancelled")
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main()
