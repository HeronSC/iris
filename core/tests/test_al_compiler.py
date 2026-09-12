# File: core/tests/test_al_compiler.py

"""The AL compiler wrapper (3.2): alc.exe found, analyzers from the workspace, diagnostics parsed."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.code_tools import ALCompileAction
from core.actions.models import ActionRequest
from core.code import CodeService
from core.code.compiler import ALCompiler, find_alc, parse_diagnostics, workspace_analyzers
from core.code.workspace import load_workspace
from core.results.models import ResultKind

OUTPUT = """
Microsoft (R) AL Compiler version 18.0.41.45789
Copyright (C) Microsoft Corporation. All rights reserved

Compilation started for project 'Mammoth Projects' containing '147' files at '10:12:01.123'.

E:\\VS\\Kloter\\src\\Codeunits\\ELEPSync.Codeunit.al(12,5): error AL0118: The name 'Foo' does not exist in the current context
E:\\VS\\Kloter\\src\\Codeunits\\ELEPSync.Codeunit.al(12,5): error AL0118: The name 'Foo' does not exist in the current context
E:\\VS\\Kloter\\src\\Pages\\ELEPPool.Page.al(40,9): warning AA0210: The FlowField or FlowFilter 'X' is not filtered.
warning AL1026: The package cache folder was not found.

Compilation ended at '10:12:20.456'.
"""


def _workspace(root: Path, *, analyzers: bool = True) -> Path:
    project = root / "Kloter"
    (project / ".vscode").mkdir(parents=True)
    (project / ".alpackages").mkdir()
    (project / "app.json").write_text(json.dumps({"name": "Mammoth Projects", "publisher": "Elephas", "version": "1.0.0.0"}), encoding="utf-8")
    if analyzers:
        (project / ".vscode" / "settings.json").write_text('{\n  // analyzers\n  "al.codeAnalyzers": ["${CodeCop}", "${UICop}", "${Nope}"],\n  "al.ruleSetPath": "_BC.ruleset.json"\n}', encoding="utf-8")
        (project / "_BC.ruleset.json").write_text("{}", encoding="utf-8")
    return project


def _extension(root: Path) -> Path:
    for version in ("ms-dynamics-smb.al-17.0.1", "ms-dynamics-smb.al-18.0.2732683", "ms-dynamics-smb.al-9.9.9"):
        folder = root / "ext" / version / "bin"
        folder.mkdir(parents=True)
        (folder / "alc.exe").write_bytes(b"MZ")
        (folder / "Microsoft.Dynamics.Nav.CodeCop.dll").write_bytes(b"x")
        (folder / "Microsoft.Dynamics.Nav.UICop.dll").write_bytes(b"x")
    return root / "ext"


class ParseAndFindTests(unittest.TestCase):
    def test_diagnostics_are_parsed_deduplicated_and_made_relative(self) -> None:
        found = parse_diagnostics(OUTPUT, Path("E:\\VS\\Kloter"))
        self.assertEqual([(item.severity, item.code) for item in found], [("error", "AL0118"), ("warning", "AA0210"), ("warning", "AL1026")])
        self.assertEqual(found[0].file, "src\\Codeunits\\ELEPSync.Codeunit.al")
        self.assertEqual((found[0].line, found[0].column), (12, 5))
        self.assertEqual(found[2].file, "")
        self.assertIn("src\\Codeunits\\ELEPSync.Codeunit.al(12,5): error AL0118", found[0].describe())

    def test_the_newest_extension_wins_and_overrides_are_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            extensions = _extension(root)
            self.assertEqual(find_alc(extensions).parent.parent.name, "ms-dynamics-smb.al-18.0.2732683")
            self.assertIsNone(find_alc(root / "missing"))
            self.assertEqual(find_alc(extensions, override=extensions / "ms-dynamics-smb.al-17.0.1" / "bin" / "alc.exe").parent.parent.name, "ms-dynamics-smb.al-17.0.1")
            self.assertIsNone(find_alc(extensions, override=root / "nowhere.exe"))

    def test_analyzers_and_ruleset_come_from_the_workspace_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alc = find_alc(_extension(root))
            workspace = load_workspace(_workspace(root))
            dlls, ruleset = workspace_analyzers(workspace, alc)
            self.assertEqual([item.name for item in dlls], ["Microsoft.Dynamics.Nav.CodeCop.dll", "Microsoft.Dynamics.Nav.UICop.dll"])
            self.assertEqual(ruleset.name, "_BC.ruleset.json")
            bare = load_workspace(_workspace(root / "bare", analyzers=False))
            self.assertEqual(workspace_analyzers(bare, alc), ([], None))


class CompileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.alc = find_alc(_extension(self.root))
        self.project = _workspace(self.root)
        self.workspace = load_workspace(self.project)
        self.commands: list[list[str]] = []

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _runner(self, code: int, output: str):
        def run(args: list[str], timeout: float, cwd: str) -> tuple[int, str]:
            self.commands.append(list(args))
            out = Path(args[3].split(":", 1)[1])
            if code == 0:
                out.write_bytes(b"NAVX")
            return code, output.replace("E:\\VS\\Kloter", str(self.project))

        return run

    def test_a_failing_build_lists_errors_and_the_command_carries_analyzers(self) -> None:
        compiler = ALCompiler(self.alc, runner=self._runner(1, OUTPUT))
        report = compiler.compile(self.workspace, out_dir=self.root / "build")
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(len(report.warnings), 2)
        self.assertIn("Mammoth Projects failed to compile: 1 error, 2 warnings", report.summary())
        self.assertIn("CodeCop, UICop", report.summary())
        command = self.commands[0]
        self.assertTrue(command[1].startswith("/project:"))
        self.assertTrue(any(item.startswith("/analyzer:") and item.endswith("CodeCop.dll") for item in command))
        self.assertTrue(any(item.startswith("/ruleset:") for item in command))
        self.assertIsNone(report.output_path)

    def test_a_clean_build_reports_the_package_and_skips_analyzers_on_request(self) -> None:
        compiler = ALCompiler(self.alc, runner=self._runner(0, "Compilation ended."))
        report = compiler.compile(self.workspace, out_dir=self.root / "build", analyzers=False)
        self.assertTrue(report.ok)
        self.assertTrue(report.output_path.endswith("Elephas_Mammoth Projects_1.0.0.0.app"))
        self.assertFalse(any(item.startswith("/analyzer:") for item in self.commands[0]))

    def test_timeouts_and_a_missing_compiler_are_reported(self) -> None:
        def slow(args: list[str], timeout: float, cwd: str) -> tuple[int, str]:
            raise subprocess.TimeoutExpired(args, timeout)

        report = ALCompiler(self.alc, runner=slow).compile(self.workspace, out_dir=self.root / "build")
        self.assertTrue(report.timed_out)
        self.assertIn("did not finish", report.summary())
        with self.assertRaises(FileNotFoundError):
            ALCompiler(self.root / "nowhere.exe").compile(self.workspace, out_dir=self.root / "build")

    def test_the_tool_returns_a_status_a_table_and_the_package(self) -> None:
        compiler = ALCompiler(self.alc, runner=self._runner(1, OUTPUT))
        service = CodeService([self.root], cache_dir=self.root / "cache" / "al_symbols", compiler=compiler)
        execution = SimpleNamespace(code_service=service)
        action = ALCompileAction()
        validation = action.validate(ActionRequest(action="al_compile", arguments={"path": str(self.project)}), execution)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="al_compile", arguments=validation.resolved_arguments), execution)
        self.assertEqual(result.status, "success")
        self.assertIn("failed to compile: 1 error, 2 warnings", result.message)
        self.assertIn("error AL0118", result.message)
        self.assertEqual([item.kind for item in result.results], [ResultKind.STATUS, ResultKind.TABLE])
        self.assertEqual(result.results[0].data["state"], "error")
        self.assertEqual(action.definition.permission.value, "execute")
        clean = CodeService([self.root], cache_dir=self.root / "cache" / "al_symbols", compiler=ALCompiler(self.alc, runner=self._runner(0, "ok")))
        built = action.execute(ActionRequest(action="al_compile", arguments={"path": str(self.project), "analyzers": True, "max_diagnostics": 40}), SimpleNamespace(code_service=clean))
        self.assertEqual([item.kind for item in built.results], [ResultKind.STATUS, ResultKind.FILE])
        missing = ALCompileAction().validate(ActionRequest(action="al_compile", arguments={}), SimpleNamespace(code_service=CodeService([self.root], compiler=ALCompiler(self.root / "nowhere.exe"))))
        self.assertFalse(missing.ok)
        self.assertIn("alc.exe was not found", missing.error)


if __name__ == "__main__":
    unittest.main()
