# File: core/code/__init__.py

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Iterable

from core.code.compiler import ALCompiler, CompileReport
from core.code.search import RipgrepSearch, SearchOutcome
from core.code.symbols import Symbol, SymbolIndex
from core.code.workspace import ALWorkspace, find_workspace_by_name, find_workspace_root, find_workspaces, load_workspace

logger = logging.getLogger(__name__)


class CodeService:
    def __init__(
        self,
        roots: Iterable[str | Path],
        *,
        cache_dir: str | Path | None = None,
        context_service: Any = None,
        search: RipgrepSearch | None = None,
        compiler: ALCompiler | None = None,
        alc_path: str | Path | None = None,
    ) -> None:
        self.roots = [Path(item) for item in roots]
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.context_service = context_service
        self.search = search or RipgrepSearch()
        self.compiler = compiler or ALCompiler(alc_path)
        self._workspaces: dict[str, ALWorkspace] = {}
        self._indexes: dict[str, SymbolIndex] = {}
        self._by_name: dict[str, Path | None] = {}
        self._lock = threading.Lock()

    def within_roots(self, path: str | Path) -> bool:
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        for root in self.roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except (OSError, ValueError):
                continue
        return False

    def workspace_at(self, path: str | Path) -> ALWorkspace | None:
        root = find_workspace_root(path)
        if root is None:
            return None
        key = str(root).lower()
        with self._lock:
            cached = self._workspaces.get(key)
            if cached is not None and (root / "app.json").stat().st_mtime <= getattr(cached, "_loaded_at", float("inf")):
                return cached
        workspace = load_workspace(root)
        if workspace is not None:
            with self._lock:
                self._workspaces[key] = workspace
        return workspace

    def workspace_named(self, name: str) -> ALWorkspace | None:
        key = name.strip().lower()
        if not key:
            return None
        with self._lock:
            known = key in self._by_name
            path = self._by_name.get(key)
        if not known:
            path = find_workspace_by_name(name, self.roots)
            with self._lock:
                self._by_name[key] = path
        return self.workspace_at(path) if path else None

    def workspaces(self) -> list[ALWorkspace]:
        found: list[ALWorkspace] = []
        for root in find_workspaces(self.roots):
            workspace = self.workspace_at(root)
            if workspace is not None:
                found.append(workspace)
        return found

    def active_workspace(self) -> ALWorkspace | None:
        service = self.context_service
        if service is None or getattr(service, "paused", False):
            return None
        try:
            current = service.current()
        except Exception:
            return None
        if current is None:
            return None
        if current.target and self.within_roots(current.target):
            workspace = self.workspace_at(current.target)
            if workspace is not None:
                return workspace
        if current.project:
            return self.workspace_named(current.project)
        return None

    def resolve_workspace(self, path: str | None) -> ALWorkspace | None:
        if path and path.strip():
            candidate = path.strip()
            if not self.within_roots(candidate):
                by_name = self.workspace_named(candidate)
                return by_name
            return self.workspace_at(candidate)
        return self.active_workspace()

    def index_for(self, workspace: ALWorkspace) -> SymbolIndex:
        key = str(workspace.root).lower()
        with self._lock:
            index = self._indexes.get(key)
            if index is None:
                index = SymbolIndex(workspace, cache_dir=self.cache_dir)
                self._indexes[key] = index
        return index

    def active_file(self) -> Path | None:
        service = self.context_service
        if service is None or getattr(service, "paused", False):
            return None
        try:
            current = service.current()
        except Exception:
            return None
        if current is None:
            return None
        if current.target and Path(current.target).is_file():
            return Path(current.target)
        file_name = (current.extra or {}).get("file") if isinstance(current.extra, dict) else None
        if file_name and current.project:
            workspace = self.workspace_named(current.project)
            if workspace is not None:
                return workspace.find_source(str(file_name))
        return None

    def prompt_line(self) -> str:
        workspace = self.active_workspace()
        if workspace is None:
            return ""
        lines = [f"- AL workspace: {workspace.name} {workspace.version} by {workspace.publisher or 'unknown'} at {workspace.root}"]
        application = workspace.app.get("application")
        if application:
            lines[0] += f", Business Central {application}"
        active = self.active_file()
        if active is not None:
            try:
                lines.append(f"- Open AL file: {active.relative_to(workspace.root)}")
            except ValueError:
                lines.append(f"- Open AL file: {active}")
        return "\n".join(lines)

    def find_symbols(self, workspace: ALWorkspace, query: str, *, kind: str | None = None, limit: int = 10) -> list[Symbol]:
        return self.index_for(workspace).find(query, kind=kind, limit=limit)

    def search_text(self, pattern: str, root: str | Path, **options: Any) -> SearchOutcome:
        return self.search.search(pattern, root, **options)

    def compile(self, workspace: ALWorkspace, *, analyzers: bool = True) -> CompileReport:
        out_dir = (self.cache_dir.parent if self.cache_dir else Path.cwd()) / "al_build"
        return self.compiler.compile(workspace, out_dir=out_dir, analyzers=analyzers)


__all__ = ["CodeService"]
