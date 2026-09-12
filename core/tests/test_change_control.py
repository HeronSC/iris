# File: core/tests/test_change_control.py

"""Change control (10): copies before writes, /undo, diffs, irreversibility, and a global stop."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.actions.audit import ActionAuditLogger
from core.actions.changes import ChangeLedger
from core.actions.executor import ActionExecutionContext, ActionExecutor, SystemAdapter
from core.actions.implementations.update_profile import UpdateProfileAction
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.audit.stream import AuditCategory, AuditStream
from core.host.service import IrisHost
from core.permissions.models import PermissionLevel, PermissionRequest
from core.permissions.policy import PermissionPolicy
from core.scheduler.models import JobDefinition, JobResult
from core.scheduler.service import ScheduleService
from core.tools.models import ToolDefinition
from core.tools.registry import ToolRegistry
from core.watchers.models import WatcherDefinition
from core.watchers.notify import LogNotifier
from core.watchers.service import WatcherService


class RewriteAction:
    name = "rewrite"
    definition = ToolDefinition(name="rewrite", description="Rewrite a file.", permission=PermissionLevel.WRITE)

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        return ValidationResult(ok=True, resolved_target=request.arguments["path"], changes=(request.arguments["path"],))

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        Path(request.arguments["path"]).write_text(request.arguments["text"], encoding="utf-8")
        return ActionResult(status="success", message="rewritten", action=self.name)


class WipeAction:
    name = "wipe"
    definition = ToolDefinition(
        name="wipe", description="Wipe the clipboard.", permission=PermissionLevel.WRITE, requires_confirmation=True, irreversible=True
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        return ValidationResult(ok=True)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        return ActionResult(status="success", message="wiped", action=self.name)


def _executor(root: Path, ledger: ChangeLedger | None, *actions: Any, context: ActionExecutionContext | None = None) -> ActionExecutor:
    registry = ActionRegistry(ToolRegistry())
    for action in actions:
        registry.register(action)
    return ActionExecutor(
        registry=registry,
        policy=ActionPolicy(),
        audit=ActionAuditLogger(root / "audit"),
        context=context
        or ActionExecutionContext(
            catalog=None,  # type: ignore[arg-type]
            allowed_roots=[],
            applications={},
            app_alias_map={},
            web_shortcuts={},
            system=SystemAdapter(),
        ),
        ledger=ledger,
    )


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.stream = AuditStream(self.root / "audit")
        self.ledger = ChangeLedger(self.root / "undo", audit=self.stream, keep=3)
        self.target = self.root / "config.json"
        self.target.write_text('{"a": 1}', encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_write_is_copied_first_and_can_be_put_back(self) -> None:
        executor = _executor(self.root, self.ledger, RewriteAction())
        result = executor.execute(ActionRequest(action="rewrite", arguments={"path": str(self.target), "text": '{"a": 2}'}))

        self.assertEqual(result.status, "success")
        self.assertIn("/undo", result.message)
        self.assertEqual(self.target.read_text(encoding="utf-8"), '{"a": 2}')

        report = self.ledger.undo()
        self.assertTrue(report.ok, report.summary)
        self.assertEqual(self.target.read_text(encoding="utf-8"), '{"a": 1}')
        self.assertEqual(self.stream.read(category=AuditCategory.ACTION)[-1].event, "undo")

    def test_a_file_that_did_not_exist_is_removed_on_undo(self) -> None:
        fresh = self.root / "new.json"
        executor = _executor(self.root, self.ledger, RewriteAction())
        executor.execute(ActionRequest(action="rewrite", arguments={"path": str(fresh), "text": "{}"}))
        self.assertTrue(fresh.exists())
        self.ledger.undo()
        self.assertFalse(fresh.exists())

    def test_a_change_is_undone_once(self) -> None:
        self.ledger.snapshot("rewrite", [self.target])
        self.assertTrue(self.ledger.undo().ok)
        second = self.ledger.undo()
        self.assertIsNone(second)

    def test_only_the_newest_copies_are_kept(self) -> None:
        for _ in range(5):
            self.ledger.snapshot("rewrite", [self.target])
        self.assertEqual(len(self.ledger.records()), 3)
        self.assertEqual(len([item for item in (self.root / "undo").iterdir() if item.is_dir()]), 3)

    def test_an_irreversible_action_says_so_before_it_runs(self) -> None:
        executor = _executor(self.root, self.ledger, WipeAction())
        result = executor.execute(ActionRequest(action="wipe", arguments={}))
        self.assertEqual(result.status, "pending_confirmation")
        self.assertIn("This cannot be undone.", result.message)

    def test_a_write_says_it_can_be_undone_before_it_runs(self) -> None:
        class ConfirmedRewrite(RewriteAction):
            definition = ToolDefinition(name="rewrite", description="Rewrite a file.", permission=PermissionLevel.WRITE, requires_confirmation=True)

        executor = _executor(self.root, self.ledger, ConfirmedRewrite())
        result = executor.execute(ActionRequest(action="rewrite", arguments={"path": str(self.target), "text": "{}"}))
        self.assertIn("/undo can put it back", result.message)
        confirmed = executor.confirm_pending()
        self.assertEqual(confirmed.status, "success")
        self.assertEqual(len(self.ledger.records()), 1)


class DiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "profile.json").write_text(json.dumps({"schema_version": 1, "profile": {"name": "Henry"}}, indent=2) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_profile_change_previews_as_a_unified_diff(self) -> None:
        context = ActionExecutionContext(
            catalog=None,  # type: ignore[arg-type]
            allowed_roots=[],
            applications={},
            app_alias_map={},
            web_shortcuts={},
            system=SystemAdapter(),
            memory_path=self.root,
        )
        validation = UpdateProfileAction().validate(ActionRequest(action="update_profile", arguments={"updates": {"city": "Boston"}}), context)

        self.assertTrue(validation.ok)
        self.assertEqual(validation.changes, (str(self.root / "profile.json"),))
        diff = validation.confirmation_preview.metadata["diff"]
        self.assertIn('+    "city": "Boston"', diff)
        self.assertIn("profile.json (now)", diff)

    def test_a_config_change_previews_the_section_it_touches_as_a_diff(self) -> None:
        #! @allow-local-import
        from core.actions.implementations.update_config import UpdateConfigAction

        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({"web_shortcuts": {"docs": "https://docs.example.com"}}, indent=2) + "\n", encoding="utf-8")
        context = ActionExecutionContext(
            catalog=None,  # type: ignore[arg-type]
            allowed_roots=[],
            applications={},
            app_alias_map={},
            web_shortcuts={},
            system=SystemAdapter(),
            config_path=config_path,
        )
        validation = UpdateConfigAction().validate(
            ActionRequest(action="update_config", arguments={"operation": "set_web_shortcut", "name": "mail", "url": "https://mail.example.com"}),
            context,
        )

        self.assertTrue(validation.ok, validation.error)
        self.assertEqual(validation.changes, (str(config_path),))
        diff = validation.confirmation_preview.metadata["diff"]
        self.assertIn('+    "mail": "https://mail.example.com"', diff)
        self.assertIn('+    "docs": "https://docs.example.com",', diff)


class PauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_paused_watchers_do_not_evaluate_and_remember_it(self) -> None:
        service = WatcherService(self.root / "w.json", self.root / "s.json", {"log": LogNotifier()})
        service.add(WatcherDefinition(kind="path_missing", params={"path": str(self.root / "nope")}, channels=("log",), id="w1"))
        service.pause()
        self.assertEqual(service.evaluate("w1"), [])
        reopened = WatcherService(self.root / "w.json", self.root / "s.json", {"log": LogNotifier()})
        self.assertTrue(reopened.paused)
        reopened.resume()
        self.assertTrue(reopened.evaluate("w1"))

    def test_paused_schedules_do_not_run(self) -> None:
        calls: list[int] = []
        service = ScheduleService(self.root / "j.json", self.root / "js.json", {"demo": lambda params: (calls.append(1), JobResult(ok=True, summary="ran"))[1]})
        service.add(JobDefinition(job="demo", interval_seconds=60, id="d1"))
        service.pause()
        self.assertIsNone(service.run("d1"))
        service.resume()
        self.assertIsNotNone(service.run("d1"))
        self.assertEqual(calls, [1])

    def test_a_halted_policy_refuses_every_tool_until_released(self) -> None:
        policy = PermissionPolicy()
        policy.halt("Iris is stopped")
        decision = policy.evaluate(PermissionRequest(tool="open_file"))
        self.assertTrue(decision.denied)
        self.assertIn("/resume", decision.reason)
        policy.release()
        self.assertTrue(policy.evaluate(PermissionRequest(tool="open_file")).allowed)


class HostControlTests(unittest.TestCase):
    def setUp(self) -> None:
        #! @allow-local-import
        from fastapi.testclient import TestClient

        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        (root / "Data" / "Memory").mkdir(parents=True)
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "assistant_name": "Iris",
                    "memory_path": str(root / "Data" / "Memory"),
                    "session_path": str(root / "Data" / "Sessions"),
                    "audit_path": str(root / "Data" / "Audit"),
                    "model": "qwen3:8b",
                    "llm_server": "http://127.0.0.1:1",
                    "llm_timeout_seconds": 1,
                    "document_search": {"catalog_path": str(root / "Data" / "Index" / "documents.db")},
                }
            ),
            encoding="utf-8",
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        self.host = IrisHost(config, http_port=port)
        self.client = TestClient(self.host.api)

    def tearDown(self) -> None:
        self.host.stop()
        self.tempdir.cleanup()

    def test_stop_and_resume_over_http_reach_every_part(self) -> None:
        stopped = self.client.post("/control/stop")
        self.assertEqual(stopped.status_code, 200)
        self.assertTrue(self.host.watchers.paused)
        self.assertTrue(self.host.schedules.paused)
        self.assertTrue(self.host.permissions.halted)

        resumed = self.client.post("/control/resume")
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(self.host.watchers.paused)
        self.assertFalse(self.host.schedules.paused)
        self.assertFalse(self.host.permissions.halted)


if __name__ == "__main__":
    unittest.main()
