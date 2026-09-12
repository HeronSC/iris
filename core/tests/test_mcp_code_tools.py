# File: core/tests/test_mcp_code_tools.py

"""Iris's code awareness over MCP (4.2): what VS Code and Copilot can call from the editor."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.code import CodeService
from core.code.compiler import ALCompiler
from core.mcp_server.code_tools import IrisCodeTools

CODEUNIT = """codeunit 99514 ELEPSync
{
    [IntegrationEvent(false, false)]
    local procedure OnBeforeSync(var Handled: Boolean)
    begin
    end;

    procedure Run()
    begin
    end;
}
"""


class _Auditor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict, str]] = []

    def record(self, name, arguments, payload, *, source="agent", kind=None):
        self.calls.append((name, dict(arguments), dict(payload), source))


class CodeToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = self.root / "Mammoth"
        (self.project / "src").mkdir(parents=True)
        (self.project / ".alpackages").mkdir()
        (self.project / "app.json").write_text(json.dumps({"name": "Mammoth Projects", "publisher": "Elephas", "version": "1.0.0.0"}), encoding="utf-8")
        (self.project / "src" / "ELEPSync.Codeunit.al").write_text(CODEUNIT, encoding="utf-8")
        self.auditor = _Auditor()
        self.tools = IrisCodeTools(CodeService([self.root], cache_dir=self.root / "cache", compiler=ALCompiler(self.root / "nowhere.exe")), auditor=self.auditor, client="vscode")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_workspaces_describe_and_symbols_answer_from_the_editor(self) -> None:
        listing = self.tools.workspaces()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["workspaces"][0]["name"], "Mammoth Projects")
        described = self.tools.describe_workspace(str(self.project))
        self.assertEqual(described["status"], "success")
        self.assertIn("Mammoth Projects 1.0.0.0 by Elephas", described["message"])
        self.assertEqual(described["results"][0]["kind"], "text")
        symbol = self.tools.find_symbol("ELEPSync", detail="events", path=str(self.project))
        self.assertIn("codeunit 99514 ELEPSync", symbol["message"])
        self.assertIn("Events: OnBeforeSync", symbol["message"])
        by_name = self.tools.describe_workspace("Mammoth Projects")
        self.assertEqual(by_name["status"], "success")

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is not installed")
    def test_search_and_read_are_confined_and_audited(self) -> None:
        found = self.tools.search_code("procedure Run", path=str(self.project))
        self.assertEqual(found["status"], "success")
        self.assertEqual(found["results"][0]["kind"], "table")
        window = self.tools.read_file(str(self.project / "src" / "ELEPSync.Codeunit.al"), start_line=8, max_lines=2)
        self.assertIn("procedure Run()", window["message"])
        outside = self.tools.read_file(str(self.root.parent / "elsewhere.txt"))
        self.assertEqual(outside["status"], "failed")
        self.assertIn("outside", outside["error"])
        self.assertEqual([call[0] for call in self.auditor.calls], ["repo_search", "read_file", "read_file"])
        self.assertTrue(all(call[3] == "vscode" for call in self.auditor.calls))

    def test_compile_without_alc_is_a_clear_failure(self) -> None:
        result = self.tools.compile_workspace(str(self.project))
        self.assertEqual(result["status"], "failed")
        self.assertIn("alc.exe was not found", result["error"])


if __name__ == "__main__":
    unittest.main()
