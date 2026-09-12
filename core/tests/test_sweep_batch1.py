# File: core/tests/test_sweep_batch1.py

"""Sweep batch one: planning models must call tools (2.4), references and open-file context (3.2), blame (3.3), project people, area and last session (3.4)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.code_tools import ALReferencesAction
from core.actions.implementations.git_tools import GitBlameAction, git_root
from core.actions.models import ActionRequest
from core.assistant.project_command import ProjectCommandHandler
from core.code import CodeService
from core.conversation.context_builder import ContextBuilder, rank_by_relevance
from core.llm.models import LLMRequest, ToolSpec
from core.llm.router import ModelRouter, ModelRoutes
from core.profile.store import MemoryStore
from core.projects.service import ProjectService

CODEUNIT = """codeunit 99514 ELEPSync
{
    [IntegrationEvent(false, false)]
    local procedure OnBeforeSync(var Handled: Boolean)
    begin
    end;

    [EventSubscriber(ObjectType::Codeunit, Codeunit::"Sales-Post", 'OnBeforePostLines', '', false, false)]
    local procedure Handle(var SalesLine: Record "Sales Line")
    begin
    end;

    procedure Run()
    begin
        Codeunit.Run(Codeunit::"Sales-Post");
    end;
}
"""


class _Client:
    def __init__(self, models: list[str], capabilities: dict[str, list[str]]) -> None:
        self.models = models
        self.caps = capabilities
        self.calls: list[str] = []

    def list_models(self) -> list[str]:
        return list(self.models)

    def show_capabilities(self, model: str) -> list[str]:
        return list(self.caps.get(model, []))

    def chat(self, request: LLMRequest):
        self.calls.append(request.model or "")
        return SimpleNamespace(content="ok", model=request.model, tool_calls=(), usage=None, prompt_tokens=0, completion_tokens=0, total_duration_ms=0.0, load_duration_ms=0.0)


class PlanningModelTests(unittest.TestCase):
    def test_a_planning_call_with_tools_moves_to_a_model_that_can_call_them(self) -> None:
        client = _Client(["mistral-small:24b", "qwen3:8b"], {"mistral-small:24b": ["completion"], "qwen3:8b": ["completion", "tools"]})
        routes = ModelRoutes(default="mistral-small:24b", tasks={"intent": "mistral-small:24b"}, fallbacks={"mistral-small:24b": ("qwen3:8b",)})
        router = ModelRouter(client, routes)
        tools = (ToolSpec(name="x", description="x", parameters={"type": "object", "properties": {}}),)
        router.chat(LLMRequest.from_prompts("s", "u", task="intent", tools=tools))
        self.assertEqual(client.calls, ["qwen3:8b"])
        client.calls.clear()
        router.chat(LLMRequest.from_prompts("s", "u", task="chat"))
        self.assertEqual(client.calls, ["mistral-small:24b"])


class ReferencesAndContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = self.root / "Mammoth"
        (self.project / "src").mkdir(parents=True)
        (self.project / ".alpackages").mkdir()
        (self.project / "app.json").write_text(json.dumps({"name": "Mammoth Projects", "publisher": "Elephas", "version": "1.0.0.0", "application": "28.0.0.0"}), encoding="utf-8")
        (self.project / "src" / "ELEPSync.Codeunit.al").write_text(CODEUNIT, encoding="utf-8")
        (self.project / "src" / "Other.Codeunit.al").write_text('codeunit 99515 Other\n{\n    procedure Go()\n    begin\n        Codeunit.Run(Codeunit::"Sales-Post");\n    end;\n}\n', encoding="utf-8")
        self.context = SimpleNamespace(paused=False, current=lambda: SimpleNamespace(target=None, project="Mammoth", extra={"file": "ELEPSync.Codeunit.al"}))
        self.service = CodeService([self.root], cache_dir=self.root / "cache", context_service=self.context)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is not installed")
    def test_references_find_quoted_and_bare_names_as_whole_words(self) -> None:
        execution = SimpleNamespace(code_service=self.service)
        action = ALReferencesAction()
        validation = action.validate(ActionRequest(action="al_references", arguments={"name": "Sales-Post", "path": str(self.project)}), execution)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="al_references", arguments=validation.resolved_arguments), execution)
        self.assertIn("3 references to Sales-Post in 2 files", result.message)
        bare = action.execute(ActionRequest(action="al_references", arguments={"name": "Run", "path": str(self.project), "max_results": 60}), execution)
        self.assertIn("references to Run in", bare.message)
        self.assertNotIn("Codeunit.Run", bare.message.split("\n")[0])
        none = action.execute(ActionRequest(action="al_references", arguments={"name": "Nowhere", "path": str(self.project), "max_results": 60}), execution)
        self.assertIn("Nothing in", none.message)

    def test_the_prompt_names_the_open_files_objects(self) -> None:
        line = self.service.prompt_line()
        self.assertIn("Open AL file: src", line)
        self.assertIn("codeunit 99514 ELEPSync: 1 public procedure: Run; publishes OnBeforeSync; subscribes to Sales-Post.OnBeforePostLines", line)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class BlameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test Author"], check=True)
        self.file = self.root / "notes.txt"
        self.file.write_text("one\ntwo\nthree\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "notes.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "first"], check=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_blame_reports_author_date_and_commit_per_line(self) -> None:
        self.assertEqual(git_root(self.file).resolve(), self.root.resolve())
        action = GitBlameAction()
        execution = SimpleNamespace(allowed_roots=[self.root])
        validation = action.validate(ActionRequest(action="git_blame", arguments={"path": str(self.file), "start_line": 2, "end_line": 3}), execution)
        self.assertTrue(validation.ok, validation.error)
        result = action.execute(ActionRequest(action="git_blame", arguments=validation.resolved_arguments), execution)
        self.assertEqual(result.status, "success")
        self.assertIn("notes.txt lines 2-3: Test Author (2)", result.message)
        rows = result.results[0].data["rows"]
        self.assertEqual([row[0] for row in rows], [2, 3])
        self.assertEqual(rows[0][4], "two")
        outside = action.validate(ActionRequest(action="git_blame", arguments={"path": str(self.root.parent / "x.txt")}), execution)
        self.assertFalse(outside.ok)


class ProjectExtrasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.memory = Path(self.tempdir.name)
        (self.memory / "projects.json").write_text(json.dumps({"schema_version": 1, "projects": [{"id": "mammoth", "name": "Mammoth", "status": "active", "paths": {}, "decisions": [{"id": "d1", "decision": "Drivers are assigned from the pool page", "status": "active"}, {"id": "d2", "decision": "Invoices post nightly", "status": "active"}], "next_actions": [], "metadata": {}}], "metadata": {}}), encoding="utf-8")
        self.service = ProjectService(self.memory)
        self.lines: list[str] = []
        self.handler = ProjectCommandHandler(output=lambda text, role=None: self.lines.append(text), service=self.service)
        self.state: dict = {"active_project_id": "mammoth"}

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, command: str) -> str:
        del self.lines[:]
        self.assertTrue(self.handler.handle(command, self.state))
        return "\n".join(self.lines)

    def test_area_people_and_last_session_reach_the_record_and_the_prompt(self) -> None:
        self.assertIn("Azure DevOps area of Mammoth: Elephas\\Mammoth", self._run("/project area Elephas\\Mammoth"))
        self.assertIn("Added Ann Lee as PM to Mammoth", self._run("/project people add Ann Lee as PM"))
        self.assertIn("Added Bob", self._run("/project people add Bob"))
        listing = self._run("/project people")
        self.assertIn("Ann Lee (PM), Bob", listing)
        project = self.service.get("mammoth")
        self.service.note_session(project, session_id="s1", title="Dispatch work", summary="Hid the driver column and agreed on nightly posting.", messages=12)
        saved = json.loads((self.memory / "projects.json").read_text(encoding="utf-8"))["projects"][0]
        self.assertEqual(saved["area_path"], "Elephas\\Mammoth")
        self.assertEqual(saved["metadata"]["last_session"]["messages"], 12)
        described = self.service.describe(project)
        self.assertIn("Azure DevOps area: Elephas\\Mammoth", described)
        self.assertIn("Last session", described)
        store = MemoryStore({"profile": {"profile": {}}, "preferences": {"preferences": []}, "projects": saved and json.loads((self.memory / "projects.json").read_text(encoding="utf-8")), "knowledge": {"knowledge_areas": []}})
        context = ContextBuilder("Iris", store).build_context("how do invoices get posted?", project_id="mammoth")
        self.assertIn("- Decisions (most relevant first): Invoices post nightly; Drivers are assigned from the pool page", context)
        self.assertIn("- Last session: Hid the driver column", context)
        self.assertEqual(rank_by_relevance(["a b c", "invoice posting"], "posting an invoice"), ["invoice posting", "a b c"])


if __name__ == "__main__":
    unittest.main()
