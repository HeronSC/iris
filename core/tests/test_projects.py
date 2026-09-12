# File: core/tests/test_projects.py

"""Project awareness (3.4): a named record with folders, tasks and decisions; inferred from what is in view."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.project_tools import ActiveProjectAction, ProjectUpdateAction
from core.actions.models import ActionRequest
from core.assistant.project_command import ProjectCommandHandler
from core.conversation.context_builder import ContextBuilder
from core.profile.store import MemoryStore
from core.projects.service import ProjectService

SEED = {
    "schema_version": 1,
    "projects": [
        {
            "id": "mammoth-dispatch",
            "name": "Mammoth Dispatch",
            "status": "active",
            "summary": "Dispatch module.",
            "technologies": ["Business Central", "AL"],
            "paths": {"repository": "", "workspace": "", "documents": ""},
            "current_focus": "Driver assignment.",
            "decisions": [{"id": "d1", "decision": "The ControlAddIn outputs JSON only.", "status": "active", "made_at": None, "source": "user"}],
            "next_actions": [],
            "metadata": {"created_at": "2026-07-28", "updated_at": "2026-07-28", "last_opened_at": None, "source": "user"},
        },
        {"id": "old-thing", "name": "Old Thing", "status": "archived", "paths": {}, "decisions": [], "next_actions": [], "metadata": {}},
    ],
    "metadata": {},
}


class _Ledger:
    def __init__(self) -> None:
        self.snapshots: list[tuple[str, list[str], str]] = []

    def snapshot(self, action: str, paths, *, reason: str = "") -> None:
        self.snapshots.append((action, [str(item) for item in paths], reason))


class _Context:
    def __init__(self, target: str | None = None, project: str | None = None) -> None:
        self.paused = False
        self._current = SimpleNamespace(target=target, project=project, extra={}, describe=lambda: f"window {project or target}")

    def current(self):
        return self._current


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.memory = Path(self.tempdir.name)
        (self.memory / "projects.json").write_text(json.dumps(SEED), encoding="utf-8")
        self.workspace = self.memory / "VS" / "Dispatch"
        self.workspace.mkdir(parents=True)
        self.ledger = _Ledger()
        self.store = MemoryStore({"projects": json.loads(json.dumps(SEED))})
        self.service = ProjectService(self.memory, store=self.store, ledger=self.ledger)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_projects_are_found_by_id_name_or_unique_part(self) -> None:
        self.assertEqual(self.service.get("mammoth-dispatch")["name"], "Mammoth Dispatch")
        self.assertEqual(self.service.get("Mammoth Dispatch")["id"], "mammoth-dispatch")
        self.assertEqual(self.service.get("dispatch")["id"], "mammoth-dispatch")
        self.assertIsNone(self.service.get("nothing"))
        self.assertEqual([item["id"] for item in self.service.active_projects()], ["mammoth-dispatch"])

    def test_writes_snapshot_first_and_reach_the_store(self) -> None:
        project = self.service.get("dispatch")
        self.service.link(project, "workspace", str(self.workspace))
        self.service.set_focus(project, "  Ship the  pool view ")
        task = self.service.add_task(project, "Write the driver test")
        decision = self.service.add_decision(project, "Keep the pool in one page")
        saved = json.loads((self.memory / "projects.json").read_text(encoding="utf-8"))
        stored = saved["projects"][0]
        self.assertEqual(stored["paths"]["workspace"], str(self.workspace))
        self.assertEqual(stored["current_focus"], "Ship the pool view")
        self.assertEqual(stored["next_actions"][0]["text"], "Write the driver test")
        self.assertEqual(task["id"], 1)
        self.assertEqual(stored["decisions"][-1]["decision"], "Keep the pool in one page")
        self.assertTrue(decision["id"].startswith("mammoth-dispatch."))
        self.assertEqual(self.store.get_project("mammoth-dispatch")["current_focus"], "Ship the pool view")
        self.assertEqual(len(self.ledger.snapshots), 4)
        self.assertTrue(all(paths == [str(self.memory / "projects.json")] for _action, paths, _reason in self.ledger.snapshots))
        self.service.complete_task(project, 1)
        self.assertEqual(self.service.open_tasks(project), [])
        with self.assertRaises(ValueError):
            self.service.complete_task(project, 9)
        with self.assertRaises(ValueError):
            self.service.link(project, "workspace", str(self.memory / "missing"))

    def test_create_makes_a_unique_id_and_refuses_duplicates(self) -> None:
        created = self.service.create("Kloter Farms BC Projects", summary="BC work")
        self.assertEqual(created["id"], "kloter-farms-bc-projects")
        with self.assertRaises(ValueError):
            self.service.create("kloter farms bc projects")
        self.assertEqual(len(self.service.projects()), 3)

    def test_inference_uses_linked_paths_then_workspace_then_window_names(self) -> None:
        project = self.service.get("dispatch")
        self.service.link(project, "workspace", str(self.workspace))
        by_path = ProjectService(self.memory, context_service=_Context(target=str(self.workspace / "src" / "a.al")))
        found, reason = by_path.infer()
        self.assertEqual(found["id"], "mammoth-dispatch")
        self.assertIn("linked folders", reason)
        code = SimpleNamespace(active_workspace=lambda: SimpleNamespace(root=self.memory / "elsewhere", name="Mammoth Dispatch"))
        by_workspace = ProjectService(self.memory, context_service=_Context(project="elsewhere"), code_service=code)
        found, reason = by_workspace.infer()
        self.assertEqual(found["id"], "mammoth-dispatch")
        self.assertIn("matches its name", reason)
        by_title = ProjectService(self.memory, context_service=_Context(project="Mammoth Dispatch"))
        self.assertEqual(by_title.infer()[0]["id"], "mammoth-dispatch")
        nothing = ProjectService(self.memory, context_service=_Context(project="Unrelated"))
        self.assertIsNone(nothing.infer()[0])
        self.assertEqual(ProjectService(self.memory).infer(), (None, "nothing is in view"))

    def test_describe_says_focus_links_decisions_and_tasks(self) -> None:
        project = self.service.get("dispatch")
        self.service.add_task(project, "Write the driver test")
        described = self.service.describe(project)
        self.assertIn("Mammoth Dispatch (mammoth-dispatch, active)", described)
        self.assertIn("Focus: Driver assignment.", described)
        self.assertIn("Decisions: The ControlAddIn outputs JSON only.", described)
        self.assertIn("Open tasks: 1. Write the driver test", described)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.memory = Path(self.tempdir.name)
        (self.memory / "projects.json").write_text(json.dumps(SEED), encoding="utf-8")
        self.service = ProjectService(self.memory, context_service=_Context(project="Mammoth Dispatch"))
        self.lines: list[str] = []
        self.handler = ProjectCommandHandler(output=lambda text, role=None: self.lines.append(text), service=self.service)
        self.sessions = SimpleNamespace(projects=[], set_project=lambda project_id: self.sessions.projects.append(project_id))
        self.state: dict = {"active_project_id": None, "session_manager": self.sessions}

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_show_switch_use_and_clear(self) -> None:
        self.assertTrue(self.handler.handle("/project", self.state))
        self.assertIn("No active project.", self.lines[0])
        self.assertIn("In view: Mammoth Dispatch (the window title names Mammoth Dispatch)", self.lines[1])
        self.handler.handle("/project use", self.state)
        self.assertEqual(self.state["active_project_id"], "mammoth-dispatch")
        self.assertEqual(self.sessions.projects, ["mammoth-dispatch"])
        self.handler.handle("/project clear", self.state)
        self.assertIsNone(self.state["active_project_id"])
        self.handler.handle("/project dispatch", self.state)
        self.assertEqual(self.state["active_project_id"], "mammoth-dispatch")
        self.assertIn("Focus: Driver assignment.", self.lines[-1])
        self.handler.handle("/project list", self.state)
        self.assertTrue(self.lines[-2].startswith("* mammoth-dispatch"))
        self.assertFalse(self.handler.handle("/other", self.state))

    def test_new_link_focus_decide_and_tasks(self) -> None:
        folder = self.memory / "Docs"
        folder.mkdir()
        self.handler.handle("/project new Bank Import", self.state)
        self.assertEqual(self.state["active_project_id"], "bank-import")
        self.handler.handle(f"/project link documents {folder}", self.state)
        self.assertIn("Linked documents of Bank Import", self.lines[-1])
        self.handler.handle("/project focus Parse the JPM file first", self.state)
        self.handler.handle("/project decide Files land in blob storage", self.state)
        self.handler.handle("/project task add Write the parser", self.state)
        self.handler.handle("/project task add Wire the nightly job", self.state)
        self.handler.handle("/project tasks", self.state)
        self.assertIn("1. Write the parser", self.lines[-1])
        self.assertIn("2. Wire the nightly job", self.lines[-1])
        self.handler.handle("/project task done 1", self.state)
        self.assertIn("Done: Write the parser", self.lines[-1])
        self.handler.handle("/project tasks", self.state)
        self.assertNotIn("Write the parser", self.lines[-1])
        saved = json.loads((self.memory / "projects.json").read_text(encoding="utf-8"))
        bank = [item for item in saved["projects"] if item["id"] == "bank-import"][0]
        self.assertEqual(bank["paths"]["documents"], str(folder))
        self.assertEqual(bank["current_focus"], "Parse the JPM file first")
        self.assertEqual(bank["decisions"][0]["decision"], "Files land in blob storage")
        self.handler.handle("/project link nowhere x", self.state)
        self.assertTrue(self.lines[-1].startswith("Usage:"))


class ToolAndPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.memory = Path(self.tempdir.name)
        (self.memory / "projects.json").write_text(json.dumps(SEED), encoding="utf-8")
        self.service = ProjectService(self.memory, context_service=_Context(project="Mammoth Dispatch"))
        self.execution = SimpleNamespace(project_service=self.service, active_project_id=lambda: None)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_active_project_reads_the_project_in_view_and_lists_tasks(self) -> None:
        self.service.add_task(self.service.get("dispatch"), "Write the driver test")
        action = ActiveProjectAction()
        validation = action.validate(ActionRequest(action="active_project", arguments={}), self.execution)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="active_project", arguments=validation.resolved_arguments), self.execution)
        self.assertEqual(result.status, "success")
        self.assertIn("Open tasks: 1. Write the driver test", result.message)
        self.assertEqual([item.title for item in result.results], ["Mammoth Dispatch", "Open tasks for Mammoth Dispatch", "Decisions for Mammoth Dispatch"])
        nothing = ActiveProjectAction().execute(ActionRequest(action="active_project", arguments={"name": "zzz"}), self.execution)
        self.assertIn("Known projects: Mammoth Dispatch", nothing.message)

    def test_project_update_previews_names_the_file_and_writes(self) -> None:
        action = ProjectUpdateAction()
        validation = action.validate(ActionRequest(action="project_update", arguments={"change": "task", "text": "Call the customer"}), self.execution)
        self.assertTrue(validation.ok)
        self.assertEqual(validation.changes, (str(self.memory / "projects.json"),))
        self.assertIn("Add task to Mammoth Dispatch: Call the customer", validation.confirmation_preview.summary)
        result = action.execute(ActionRequest(action="project_update", arguments=validation.resolved_arguments), self.execution)
        self.assertEqual(result.status, "success")
        self.assertEqual(self.service.open_tasks(self.service.get("dispatch"))[0]["text"], "Call the customer")
        done = action.validate(ActionRequest(action="project_update", arguments={"change": "task_done", "text": "1"}), self.execution)
        self.assertTrue(done.ok)
        action.execute(ActionRequest(action="project_update", arguments=done.resolved_arguments), self.execution)
        self.assertEqual(self.service.open_tasks(self.service.get("dispatch")), [])
        bad = action.validate(ActionRequest(action="project_update", arguments={"change": "task_done", "text": "one"}), self.execution)
        self.assertFalse(bad.ok)
        self.assertEqual(action.definition.permission.value, "write")

    def test_the_prompt_carries_decisions_and_open_tasks(self) -> None:
        self.service.add_task(self.service.get("dispatch"), "Write the driver test")
        store = MemoryStore({"profile": {"profile": {}}, "preferences": {"preferences": []}, "projects": json.loads((self.memory / "projects.json").read_text(encoding="utf-8")), "knowledge": {"knowledge_areas": []}})
        context = ContextBuilder("Iris", store).build_context("what next?", project_id="mammoth-dispatch")
        self.assertIn("Active project:", context)
        self.assertIn("- Decisions: The ControlAddIn outputs JSON only.", context)
        self.assertIn("- Open tasks: 1. Write the driver test", context)


if __name__ == "__main__":
    unittest.main()
