# File: core/tests/test_tools.py

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.actions.audit import ActionAuditLogger
from core.actions.executor import ActionExecutionContext, ActionExecutor, SystemAdapter
from core.actions.implementations.launch_application import LaunchApplicationAction
from core.actions.implementations.open_url import OpenUrlAction
from core.actions.implementations.update_config import UpdateConfigAction
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.tools.models import PermissionLevel, ToolArgumentError, ToolDefinition, ToolKind
from core.tools.registry import ToolRegistry


class GreetArguments(BaseModel):
    name: str = Field(description="Who to greet")
    excited: bool = False


class GreetAction:
    name = "greet"
    definition = ToolDefinition(
        name="greet",
        description="Say hello.",
        arguments=GreetArguments,
        permission=PermissionLevel.READ,
    )
    facets = (
        ToolDefinition(
            name="greet_loudly",
            description="Say hello with enthusiasm.",
            arguments=GreetArguments,
            bind={"excited": True},
            requires_confirmation=True,
        ),
    )

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        if not str(request.arguments.get("name", "")).strip():
            return ValidationResult(ok=False, error="Missing name")
        return ValidationResult(ok=True, resolved_target=str(request.arguments["name"]))

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        self.executed.append(dict(request.arguments))
        return ActionResult(status="success", message="hi", action=self.name)


class ToolDefinitionTests(unittest.TestCase):
    def test_schema_comes_from_the_pydantic_model(self) -> None:
        schema = GreetAction.definition.parameters_schema()
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["name"])
        self.assertEqual(schema["properties"]["name"]["description"], "Who to greet")
        self.assertNotIn("title", schema)
        spec = GreetAction.definition.to_spec().to_ollama()
        self.assertEqual(spec["function"]["name"], "greet")
        self.assertEqual(spec["function"]["parameters"], schema)

    def test_validate_arguments_coerces_and_rejects(self) -> None:
        self.assertEqual(
            GreetAction.definition.validate_arguments({"name": "Henry", "excited": "true", "extra": 1}),
            {"name": "Henry", "excited": True},
        )
        with self.assertRaises(ToolArgumentError) as error:
            GreetAction.definition.validate_arguments({})
        self.assertIn("name", str(error.exception))

    def test_raw_schema_definition_needs_no_model(self) -> None:
        definition = ToolDefinition(
            name="weather",
            description="Weather lookup",
            parameters={"type": "object", "properties": {"location": {"type": "string"}}},
            kind=ToolKind.CAPABILITY,
        )
        self.assertEqual(definition.parameters_schema()["properties"]["location"]["type"], "string")
        self.assertEqual(definition.validate_arguments({"location": "Boston"}), {"location": "Boston"})


class ToolRegistryTests(unittest.TestCase):
    def test_action_registry_declares_action_and_facets_once(self) -> None:
        tools = ToolRegistry()
        registry = ActionRegistry(tools)
        action = GreetAction()
        registry.register(action)

        self.assertEqual(registry.names(), ["greet"])
        self.assertEqual(tools.names(), ["greet", "greet_loudly"])
        self.assertIs(registry.get("greet"), action)
        self.assertIs(registry.get("greet_loudly"), action)
        facet = tools.get("greet_loudly")
        assert facet is not None
        self.assertTrue(facet.is_facet)
        self.assertEqual(facet.target_action, "greet")
        with self.assertRaises(ValueError):
            registry.register(GreetAction())

    def test_facet_resolves_onto_the_underlying_action(self) -> None:
        registry = ActionRegistry()
        registry.register(GreetAction())
        resolved = registry.resolve(ActionRequest(action="greet_loudly", arguments={"name": "Henry"}, reason="test"))
        assert resolved is not None
        self.assertIsNone(resolved.error)
        self.assertEqual(resolved.request.action, "greet")
        self.assertEqual(resolved.request.arguments, {"name": "Henry", "excited": True})
        self.assertEqual(resolved.request.reason, "test")

        bad = registry.resolve(ActionRequest(action="greet_loudly", arguments={}))
        assert bad is not None
        self.assertIn("name", bad.error or "")
        self.assertIsNone(registry.resolve(ActionRequest(action="nope", arguments={})))

    def test_facet_cannot_bind_to_unknown_action(self) -> None:
        tools = ToolRegistry()
        with self.assertRaises(ValueError):
            tools.register(ToolDefinition(name="x", description="x", action="missing"))

    def test_model_tools_respect_exposure_and_enablement(self) -> None:
        tools = ToolRegistry()
        registry = ActionRegistry(tools)
        registry.register(GreetAction())
        tools.register(ToolDefinition(name="hidden", description="not for the model", expose_to_model=False))

        self.assertEqual([spec.name for spec in tools.model_tools()], ["greet", "greet_loudly"])
        tools.disable("greet")
        self.assertFalse(tools.is_enabled("greet"))
        self.assertFalse(tools.is_enabled("greet_loudly"), "a facet follows its action")
        self.assertEqual(tools.model_tools(), ())
        tools.enable("greet")
        self.assertEqual(len(tools.model_tools()), 2)
        with self.assertRaises(KeyError):
            tools.disable("missing")

    def test_describe_lists_every_tool(self) -> None:
        tools = ToolRegistry()
        ActionRegistry(tools).register(GreetAction())
        rows = {row["name"]: row for row in tools.describe()}
        self.assertEqual(rows["greet_loudly"]["action"], "greet")
        self.assertTrue(rows["greet_loudly"]["requires_confirmation"])
        self.assertEqual(rows["greet"]["permission"], "read")

    def test_real_actions_expose_the_same_intents_the_classifier_had(self) -> None:
        registry = ActionRegistry()
        registry.register(LaunchApplicationAction())
        registry.register(OpenUrlAction())
        registry.register(UpdateConfigAction())
        exposed = {spec.name for spec in registry.tools.model_tools()}
        self.assertEqual(
            exposed,
            {
                "launch_application",
                "open_url_shortcut",
                "config_add_application",
                "config_set_document_roots",
                "config_remove_document_root",
                "config_set_web_shortcut",
                "config_remove_web_shortcut",
                "config_remove_application",
                "config_set_value",
            },
        )
        resolved = registry.resolve(
            ActionRequest(action="config_set_value", arguments={"key": "model", "value": "qwen3:8b"})
        )
        assert resolved is not None
        self.assertEqual(resolved.request.action, "update_config")
        self.assertEqual(resolved.request.arguments, {"key": "model", "value": "qwen3:8b", "operation": "set_value"})


class ExecutorToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.audit = ActionAuditLogger(Path(self.tempdir.name))
        self.tools = ToolRegistry()
        self.registry = ActionRegistry(self.tools)
        self.action = GreetAction()
        self.registry.register(self.action)
        self.executor = ActionExecutor(
            registry=self.registry,
            policy=ActionPolicy(),
            audit=self.audit,
            context=ActionExecutionContext(
                catalog=None,  # type: ignore[arg-type]
                allowed_roots=[],
                applications={},
                app_alias_map={},
                web_shortcuts={},
                system=SystemAdapter(),
            ),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _audit_entries(self) -> list[dict[str, Any]]:
        """Read back through the logger: the file itself is the shared schema now (2.8)."""
        return self.audit.read_recent(limit=50)

    def test_declared_confirmation_gates_a_facet_and_flattens_it(self) -> None:
        result = self.executor.execute(ActionRequest(action="greet_loudly", arguments={"name": "Henry"}))
        self.assertEqual(result.status, "pending_confirmation")
        self.assertEqual(self.action.executed, [])

        confirmed = self.executor.confirm_pending()
        self.assertEqual(confirmed.status, "success")
        self.assertEqual(self.action.executed, [{"name": "Henry", "excited": True}])

        entries = self._audit_entries()
        self.assertEqual(entries[0]["tool"], "greet_loudly")
        self.assertEqual(entries[0]["action"], "greet")
        self.assertEqual(entries[0]["status"], "pending_confirmation")

    def test_direct_action_without_confirmation_runs(self) -> None:
        result = self.executor.execute(ActionRequest(action="greet", arguments={"name": "Henry"}))
        self.assertEqual(result.status, "success")
        self.assertEqual(self.action.executed, [{"name": "Henry"}])
        self.assertEqual(self._audit_entries()[0]["tool"], "greet")

    def test_invalid_facet_arguments_fail_before_validate(self) -> None:
        result = self.executor.execute(ActionRequest(action="greet_loudly", arguments={}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "invalid_arguments")
        self.assertIn("name", result.message)

    def test_disabled_tool_is_refused(self) -> None:
        self.tools.disable("greet")
        result = self.executor.execute(ActionRequest(action="greet", arguments={"name": "Henry"}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "tool_disabled")
        self.assertEqual(self.action.executed, [])

    def test_unknown_tool_is_reported(self) -> None:
        result = self.executor.execute(ActionRequest(action="missing", arguments={}))
        self.assertEqual(result.error, "unknown_action")


if __name__ == "__main__":
    unittest.main()
