# File: core/tests/test_context.py

"""Active context awareness (3.1): what the user is looking at, one provider per app, pausable."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.active_context import ActiveContextAction
from core.actions.models import ActionRequest
from core.assistant.context_command import ContextCommandHandler
from core.context.models import WindowInfo
from core.context.providers import ExcelProvider, ExplorerProvider, VSCodeProvider, WindowProvider, default_providers
from core.context.service import ContextService
from core.results.models import ResultKind

WORKBOOK = "E:" + chr(92) + "Docs" + chr(92) + "budget.xlsx"


def _window(title: str, process: str, pid: int = 4242) -> WindowInfo:
    return WindowInfo(handle=1, title=title, process_name=process, pid=pid)


class _FakeSelection:
    def Address(self, row_absolute: bool, column_absolute: bool) -> str:
        return "B2:D9"


class _FakeExcel:
    ActiveWorkbook = SimpleNamespace(FullName=WORKBOOK)
    ActiveSheet = SimpleNamespace(Name="Q3")
    Selection = _FakeSelection()


class ProviderTests(unittest.TestCase):
    def test_vscode_titles_give_file_project_and_unsaved_state(self) -> None:
        provider = VSCodeProvider()
        dirty = provider.capture(_window("● Cust.Codeunit.al - MyApp - Visual Studio Code", "Code.exe"))
        self.assertEqual(dirty.app, "VS Code")
        self.assertEqual(dirty.project, "MyApp")
        self.assertEqual(dirty.extra["file"], "Cust.Codeunit.al")
        self.assertTrue(dirty.extra["unsaved"])
        self.assertIsNone(dirty.target)
        clean = provider.capture(_window("MyApp - Visual Studio Code", "Code.exe"))
        self.assertEqual(clean.project, "MyApp")
        self.assertIn("MyApp", clean.describe())
        self.assertTrue(provider.matches(_window("x - Visual Studio Code", "Code.exe")))
        self.assertFalse(provider.matches(_window("Inbox", "olk.exe")))

    def test_excel_uses_com_for_the_workbook_sheet_and_selection(self) -> None:
        provider = ExcelProvider(lambda: _FakeExcel())
        captured = provider.capture(_window("budget.xlsx - Excel", "EXCEL.EXE"))
        self.assertEqual(captured.target, WORKBOOK)
        self.assertEqual(captured.target_kind, "workbook")
        self.assertEqual(captured.selection, "Q3!B2:D9")
        self.assertEqual(captured.extra["sheet"], "Q3")
        self.assertIn("workbook " + WORKBOOK, captured.describe())
        self.assertIn("selection Q3!B2:D9", captured.describe())

    def test_excel_falls_back_to_the_title_when_com_is_unavailable(self) -> None:
        provider = ExcelProvider(lambda: None)
        captured = provider.capture(_window("budget.xlsx - Excel", "EXCEL.EXE"))
        self.assertEqual(captured.target, "budget.xlsx")
        self.assertEqual(captured.target_kind, "workbook")

    def test_explorer_and_plain_windows(self) -> None:
        folder = ExplorerProvider().capture(_window("E:" + chr(92) + "Docs", "explorer.exe"))
        self.assertEqual(folder.target_kind, "folder")
        named = ExplorerProvider().capture(_window("Downloads", "explorer.exe"))
        self.assertIsNone(named.target)
        self.assertEqual(named.extra["folder_name"], "Downloads")
        plain = WindowProvider().capture(_window("Containers - Docker Desktop", "Docker Desktop.exe"))
        self.assertEqual(plain.app, "Docker Desktop")
        self.assertIn('"Containers - Docker Desktop"', plain.describe())

    def test_the_default_chain_ends_with_the_plain_window(self) -> None:
        providers = default_providers(lambda: None)
        self.assertEqual(providers[-1].name, "window")
        self.assertEqual([provider.name for provider in providers][:2], ["vscode", "excel"])


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.windows: list[WindowInfo | None] = []
        self.service = ContextService(default_providers(lambda: _FakeExcel()), foreground=self._next, own_pid=999, poll_seconds=0.2, history_size=3)

    def _next(self) -> WindowInfo | None:
        return self.windows.pop(0) if self.windows else None

    def test_history_records_changes_only_and_skips_iris_itself(self) -> None:
        self.windows = [
            _window("MyApp - Visual Studio Code", "Code.exe"),
            _window("MyApp - Visual Studio Code", "Code.exe"),
            _window("Iris", "python.exe", pid=999),
            _window("budget.xlsx - Excel", "EXCEL.EXE"),
        ]
        for _ in range(4):
            self.service.sample()
        history = self.service.history()
        self.assertEqual([entry.app for entry in history], ["Excel", "VS Code"])
        self.assertEqual(self.service.previous().app, "VS Code")

    def test_current_samples_once_more_and_resolve_understands_this_and_before(self) -> None:
        self.windows = [_window("MyApp - Visual Studio Code", "Code.exe"), _window("budget.xlsx - Excel", "EXCEL.EXE")]
        self.service.sample()
        current = self.service.current()
        self.assertEqual(current.app, "Excel")
        picked, why = self.service.resolve("look at this workbook")
        self.assertEqual(picked.target, WORKBOOK)
        self.assertIn("workbook", why)
        before, why_before = self.service.resolve("the file I had open before")
        self.assertEqual(before.app, "VS Code")
        self.assertEqual(why_before, "the one before it")

    def test_pause_stops_sampling_and_the_prompt_line(self) -> None:
        self.windows = [_window("budget.xlsx - Excel", "EXCEL.EXE")]
        self.service.sample()
        self.assertIn("Now: Excel", self.service.prompt_line())
        self.service.pause()
        self.windows = [_window("MyApp - Visual Studio Code", "Code.exe")]
        self.assertEqual(self.service.prompt_line(), "")
        self.assertEqual(self.service.current().app, "Excel")
        picked, why = self.service.resolve("this")
        self.assertEqual(picked.app, "Excel")
        self.service.resume()
        self.assertEqual(self.service.current().app, "VS Code")

    def test_a_broken_provider_does_not_stop_the_chain(self) -> None:
        class Broken:
            name = "broken"

            def matches(self, window: WindowInfo) -> bool:
                return True

            def capture(self, window: WindowInfo):
                raise RuntimeError("boom")

        service = ContextService([Broken(), WindowProvider()], foreground=lambda: _window("Notepad", "notepad.exe"), own_pid=999)
        captured = service.sample()
        self.assertEqual(captured.app, "notepad")
        self.assertIn("boom", service.last_error)

    def test_the_sampler_thread_starts_and_stops(self) -> None:
        self.windows = [_window("budget.xlsx - Excel", "EXCEL.EXE")] * 5
        self.service.start()
        self.assertTrue(self.service.running)
        self.service.stop()
        self.assertFalse(self.service.running)


class CommandAndToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workbook = Path(self.tempdir.name) / "budget.xlsx"
        self.workbook.write_bytes(b"x" * 10)
        excel = SimpleNamespace(ActiveWorkbook=SimpleNamespace(FullName=str(self.workbook)), ActiveSheet=SimpleNamespace(Name="Q3"), Selection=_FakeSelection())
        self.windows = [_window("MyApp - Visual Studio Code", "Code.exe"), _window("budget.xlsx - Excel", "EXCEL.EXE")]
        self.service = ContextService(default_providers(lambda: excel), foreground=lambda: self.windows.pop(0) if self.windows else None, own_pid=999)
        self.lines: list[str] = []
        self.handler = ContextCommandHandler(self.service, output=lambda text, role=None: self.lines.append(text))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_command_shows_pauses_and_resumes(self) -> None:
        self.service.sample()
        self.assertTrue(self.handler.handle("/context", {}))
        self.assertIn("Now: Excel", self.lines[-1])
        self.assertIn("Before that: VS Code", self.lines[-1])
        self.handler.handle("/context history", {})
        self.assertIn("1. Excel", self.lines[-1])
        self.handler.handle("/context pause", {})
        self.assertTrue(self.service.paused)
        self.handler.handle("/context", {})
        self.assertIn("paused", self.lines[-1])
        self.handler.handle("/context resume", {})
        self.assertFalse(self.service.paused)
        self.handler.handle("/context nonsense", {})
        self.assertIn("Usage", self.lines[-1])
        self.assertFalse(self.handler.handle("/other", {}))

    def test_the_tool_returns_the_file_and_a_sentence(self) -> None:
        action = ActiveContextAction()
        context = SimpleNamespace(context_service=self.service)
        self.service.sample()
        validation = action.validate(ActionRequest(action="active_context", arguments={}), context)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="active_context", arguments=validation.resolved_arguments), context)
        self.assertEqual(result.status, "success")
        self.assertIn("The user is looking at Excel, workbook", result.message)
        self.assertEqual(result.resolved_target, str(self.workbook))
        self.assertEqual([item.kind for item in result.results], [ResultKind.FILE, ResultKind.TEXT])
        self.assertEqual(result.results[0].data["size_bytes"], 10)
        previous = action.execute(ActionRequest(action="active_context", arguments={"which": "previous"}), context)
        self.assertIn("VS Code", previous.message)
        history = action.execute(ActionRequest(action="active_context", arguments={"which": "history"}), context)
        self.assertIn("1. Excel", history.message)

    def test_the_tool_says_when_capture_is_missing_or_paused(self) -> None:
        action = ActiveContextAction()
        missing = action.execute(ActionRequest(action="active_context", arguments={"which": "current"}), SimpleNamespace())
        self.assertEqual(missing.error, "context_unavailable")
        self.service.pause()
        paused = action.execute(ActionRequest(action="active_context", arguments={"which": "current"}), SimpleNamespace(context_service=self.service))
        self.assertEqual(paused.error, "context_paused")
        self.assertIn("/context resume", paused.message)
        self.assertEqual(action.definition.permission.value, "read")
        bad = action.validate(ActionRequest(action="active_context", arguments={"which": "future"}), SimpleNamespace())
        self.assertFalse(bad.ok)


if __name__ == "__main__":
    unittest.main()
