# File: core/mcp_server/code_tools.py

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from core.actions.implementations.code_tools import ALCompileAction, ALSymbolAction, ALWorkspaceAction, RepoSearchAction
from core.actions.implementations.file_tools import ReadFileAction
from core.actions.models import ActionRequest
from core.code import CodeService
from core.results.models import to_json_list


class IrisCodeTools:
    def __init__(self, code_service: CodeService, *, allowed_roots: Iterable[str | Path] = (), auditor: Any = None, client: str = "mcp") -> None:
        self.code_service = code_service
        self.allowed_roots = [Path(item) for item in allowed_roots] or list(code_service.roots)
        self.auditor = auditor
        self.client = client
        self.context = SimpleNamespace(code_service=code_service, allowed_roots=self.allowed_roots)

    def _run(self, action: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        request = ActionRequest(action=action.name, arguments={key: value for key, value in arguments.items() if value is not None})
        validation = action.validate(request, self.context)
        if not validation.ok:
            payload = {"status": "failed", "error": validation.error, "message": validation.error or "refused"}
        else:
            result = action.execute(ActionRequest(action=action.name, arguments=validation.resolved_arguments or request.arguments), self.context)
            payload = {"status": result.status, "message": result.message, "results": to_json_list(result.results)}
            if result.error:
                payload["error"] = result.error
        if self.auditor is not None:
            try:
                self.auditor.record(action.name, arguments, payload, source=self.client)
            except Exception:
                pass
        return payload

    def search_code(self, pattern: str, path: str | None = None, glob: str | None = None, regex: bool = False, max_results: int = 40) -> dict[str, Any]:
        return self._run(RepoSearchAction(), {"pattern": pattern, "path": path, "glob": glob, "regex": regex, "max_results": max_results})

    def describe_workspace(self, path: str | None = None) -> dict[str, Any]:
        return self._run(ALWorkspaceAction(), {"path": path})

    def find_symbol(self, name: str, kind: str | None = None, detail: str = "summary", path: str | None = None) -> dict[str, Any]:
        return self._run(ALSymbolAction(), {"name": name, "kind": kind, "detail": detail, "path": path})

    def compile_workspace(self, path: str | None = None, analyzers: bool = True, max_diagnostics: int = 40) -> dict[str, Any]:
        return self._run(ALCompileAction(), {"path": path, "analyzers": analyzers, "max_diagnostics": max_diagnostics})

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> dict[str, Any]:
        return self._run(ReadFileAction(), {"path": path, "start_line": start_line, "max_lines": max_lines})

    def workspaces(self) -> dict[str, Any]:
        found = [workspace.to_json() for workspace in self.code_service.workspaces()]
        return {"status": "success", "count": len(found), "workspaces": found}


__all__ = ["IrisCodeTools"]
