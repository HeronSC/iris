# File: core/code/compiler.py

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.code.workspace import ALWorkspace

logger = logging.getLogger(__name__)

EXTENSION_GLOB = "ms-dynamics-smb.al-*"
ANALYZER_DLLS = {
    "codecop": "Microsoft.Dynamics.Nav.CodeCop.dll",
    "uicop": "Microsoft.Dynamics.Nav.UICop.dll",
    "pertenantextensioncop": "Microsoft.Dynamics.Nav.PerTenantExtensionCop.dll",
    "appsourcecop": "Microsoft.Dynamics.Nav.AppSourceCop.dll",
}
DIAGNOSTIC = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s+(?P<severity>error|warning|info)\s+(?P<code>[A-Z]{2}\d{4}):\s+(?P<message>.*)$", re.IGNORECASE)
BARE_DIAGNOSTIC = re.compile(r"^(?P<severity>error|warning)\s+(?P<code>[A-Z]{2}\d{4}):\s+(?P<message>.*)$", re.IGNORECASE)
DEFAULT_TIMEOUT_SECONDS = 600.0

Runner = Callable[[list[str], float, str], tuple[int, str]]


def _version_key(path: Path) -> tuple[int, ...]:
    tail = path.name.split("al-", 1)[-1]
    parts: list[int] = []
    for piece in tail.split("."):
        digits = re.sub(r"\D", "", piece)
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def find_alc(extensions_dir: str | Path | None = None, *, override: str | Path | None = None) -> Path | None:
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    base = Path(extensions_dir).expanduser() if extensions_dir else Path.home() / ".vscode" / "extensions"
    if not base.is_dir():
        return None
    found = sorted((folder for folder in base.glob(EXTENSION_GLOB) if (folder / "bin" / "alc.exe").is_file()), key=_version_key)
    return found[-1] / "bin" / "alc.exe" if found else None


def default_runner(args: list[str], timeout: float, cwd: str) -> tuple[int, str]:
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, encoding="utf-8", errors="replace", check=False)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    file: str = ""
    line: int | None = None
    column: int | None = None

    @property
    def location(self) -> str:
        if not self.file:
            return ""
        return f"{self.file}({self.line},{self.column})" if self.line is not None else self.file

    def describe(self) -> str:
        where = f"{self.location}: " if self.file else ""
        return f"{where}{self.severity} {self.code}: {self.message}"


@dataclass(frozen=True)
class CompileReport:
    workspace: str
    ok: bool
    diagnostics: tuple[Diagnostic, ...]
    elapsed_seconds: float
    command: tuple[str, ...]
    output_path: str | None = None
    raw: str = ""
    timed_out: bool = False
    analyzers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "warning")

    def summary(self) -> str:
        if self.timed_out:
            return f"Compiling {self.workspace} did not finish in time."
        outcome = "compiled" if self.ok else "failed to compile"
        return f"{self.workspace} {outcome}: {len(self.errors)} error{'s' if len(self.errors) != 1 else ''}, {len(self.warnings)} warning{'s' if len(self.warnings) != 1 else ''} in {self.elapsed_seconds:.0f} s" + (f" with {', '.join(self.analyzers)}" if self.analyzers else "")


def parse_diagnostics(output: str, root: Path | None = None) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    seen: set[tuple[str, int | None, str, str]] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = DIAGNOSTIC.match(line)
        if match:
            file_name = match.group("file")
            if root is not None:
                try:
                    file_name = str(Path(file_name).relative_to(root))
                except ValueError:
                    pass
            item = Diagnostic(severity=match.group("severity").lower(), code=match.group("code").upper(), message=match.group("message").strip(), file=file_name, line=int(match.group("line")), column=int(match.group("column")))
        else:
            bare = BARE_DIAGNOSTIC.match(line)
            if not bare:
                continue
            item = Diagnostic(severity=bare.group("severity").lower(), code=bare.group("code").upper(), message=bare.group("message").strip())
        key = (item.file, item.line, item.code, item.message)
        if key in seen:
            continue
        seen.add(key)
        found.append(item)
    return found


def workspace_analyzers(workspace: ALWorkspace, alc_path: Path) -> tuple[list[Path], Path | None]:
    settings_path = workspace.root / ".vscode" / "settings.json"
    names: list[str] = []
    ruleset: Path | None = None
    if settings_path.is_file():
        try:
            settings = json.loads(re.sub(r"^\s*//.*$", "", settings_path.read_text(encoding="utf-8-sig"), flags=re.MULTILINE))
        except (OSError, ValueError):
            settings = {}
        if isinstance(settings, dict):
            raw = settings.get("al.codeAnalyzers")
            if isinstance(raw, list):
                names = [str(item) for item in raw]
            rule_path = settings.get("al.ruleSetPath")
            if rule_path:
                candidate = (workspace.root / str(rule_path)).resolve() if not Path(str(rule_path)).is_absolute() else Path(str(rule_path))
                ruleset = candidate if candidate.is_file() else None
    dlls: list[Path] = []
    for name in names:
        key = re.sub(r"[^a-z]", "", name.lower())
        dll = ANALYZER_DLLS.get(key)
        if dll is None:
            continue
        candidate = alc_path.parent / dll
        if candidate.is_file():
            dlls.append(candidate)
    return dlls, ruleset


class ALCompiler:
    def __init__(self, alc_path: str | Path | None = None, *, runner: Runner = default_runner, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.alc_path = Path(alc_path) if alc_path else find_alc()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return self.alc_path is not None and Path(self.alc_path).is_file()

    def compile(self, workspace: ALWorkspace, *, out_dir: str | Path, analyzers: bool = True) -> CompileReport:
        if not self.available:
            raise FileNotFoundError("alc.exe was not found; install the AL Language extension for VS Code or set code.alc_path in config.json")
        assert self.alc_path is not None
        output_folder = Path(out_dir).expanduser()
        output_folder.mkdir(parents=True, exist_ok=True)
        output_path = output_folder / f"{workspace.publisher or 'app'}_{workspace.name}_{workspace.version or '0'}.app".replace("/", "-")
        command = [str(self.alc_path), f"/project:{workspace.root}", f"/packagecachepath:{workspace.root / '.alpackages'}", f"/out:{output_path}", "/loglevel:Warning"]
        used: list[str] = []
        if analyzers:
            dlls, ruleset = workspace_analyzers(workspace, Path(self.alc_path))
            for dll in dlls:
                command.append(f"/analyzer:{dll}")
                used.append(dll.name.replace("Microsoft.Dynamics.Nav.", "").replace(".dll", ""))
            if ruleset is not None:
                command.append(f"/ruleset:{ruleset}")
        started = time.perf_counter()
        try:
            code, output = self.runner(command, self.timeout_seconds, str(workspace.root))
        except subprocess.TimeoutExpired:
            return CompileReport(workspace=workspace.name, ok=False, diagnostics=(), elapsed_seconds=time.perf_counter() - started, command=tuple(command), raw="", timed_out=True, analyzers=tuple(used))
        elapsed = time.perf_counter() - started
        diagnostics = parse_diagnostics(output, workspace.root)
        errors = [item for item in diagnostics if item.severity == "error"]
        ok = code == 0 and not errors
        return CompileReport(
            workspace=workspace.name,
            ok=ok,
            diagnostics=tuple(diagnostics),
            elapsed_seconds=elapsed,
            command=tuple(command),
            output_path=str(output_path) if ok and output_path.is_file() else None,
            raw=output[-4000:],
            analyzers=tuple(used),
        )


__all__ = ["ALCompiler", "ANALYZER_DLLS", "CompileReport", "Diagnostic", "default_runner", "find_alc", "parse_diagnostics", "workspace_analyzers"]
