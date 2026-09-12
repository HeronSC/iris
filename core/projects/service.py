# File: core/projects/service.py

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PATH_KINDS = ("workspace", "repository", "documents")
PROJECTS_FILE = "projects.json"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def _within(path: Path, root: str) -> bool:
    if not root:
        return False
    try:
        path.resolve().relative_to(Path(root).expanduser().resolve())
        return True
    except (OSError, ValueError):
        return False


class ProjectService:
    def __init__(self, memory_folder: str | Path | None, *, store: Any = None, ledger: Any = None, context_service: Any = None, code_service: Any = None) -> None:
        self.path = Path(memory_folder).expanduser() / PROJECTS_FILE if memory_folder else None
        self.store = store
        self.ledger = ledger
        self.context_service = context_service
        self.code_service = code_service
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            payload: Any = None
            if self.store is not None and callable(getattr(self.store, "to_dict", None)):
                section = self.store.to_dict().get("projects")
                if isinstance(section, dict) and isinstance(section.get("projects"), list):
                    payload = section
            if payload is None:
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8-sig")) if self.path is not None and self.path.is_file() else {}
                except (OSError, ValueError) as error:
                    logger.warning("Could not read %s: %s", self.path, error)
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("schema_version", 1)
            payload.setdefault("projects", [])
            payload.setdefault("metadata", {})
            self._data = payload
        return self._data

    def _save(self, reason: str) -> None:
        data = self._load()
        if self.ledger is not None and self.path is not None:
            try:
                self.ledger.snapshot("project", [self.path], reason=reason)
            except Exception as error:
                logger.debug("Project snapshot not taken: %s", error)
        data.setdefault("metadata", {})["updated_at"] = _today()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if self.store is not None and hasattr(self.store, "replace_section"):
            self.store.replace_section("projects", data)

    def projects(self) -> list[dict[str, Any]]:
        return list(self._load()["projects"])

    def active_projects(self) -> list[dict[str, Any]]:
        return [item for item in self.projects() if item.get("status", "active") == "active"]

    def get(self, reference: str | None) -> dict[str, Any] | None:
        needle = str(reference or "").strip().casefold()
        if not needle:
            return None
        projects = self.projects()
        for project in projects:
            if str(project.get("id", "")).casefold() == needle or str(project.get("name", "")).casefold() == needle:
                return project
        partial = [project for project in projects if needle in str(project.get("name", "")).casefold() or needle in str(project.get("id", "")).casefold()]
        return partial[0] if len(partial) == 1 else None

    def create(self, name: str, *, summary: str = "", technologies: tuple[str, ...] = ()) -> dict[str, Any]:
        clean = " ".join(name.split())
        if not clean:
            raise ValueError("A project needs a name")
        if self.get(clean) is not None:
            raise ValueError(f"A project named {clean} already exists")
        base = _slug(clean)
        identifier = base
        counter = 2
        while any(str(item.get("id")) == identifier for item in self.projects()):
            identifier = f"{base}-{counter}"
            counter += 1
        project = {
            "id": identifier,
            "name": clean,
            "status": "active",
            "summary": summary,
            "technologies": list(technologies),
            "paths": {kind: "" for kind in PATH_KINDS},
            "current_focus": "",
            "architecture": [],
            "decisions": [],
            "next_actions": [],
            "metadata": {"created_at": _today(), "updated_at": _today(), "last_opened_at": None, "source": "user"},
        }
        with self._lock:
            self._load()["projects"].append(project)
            self._save(f"create project {clean}")
        return project

    def link(self, project: dict[str, Any], kind: str, path: str) -> dict[str, Any]:
        if kind not in PATH_KINDS:
            raise ValueError(f"kind is one of {', '.join(PATH_KINDS)}")
        target = Path(path).expanduser()
        if not target.exists():
            raise ValueError(f"{path} does not exist")
        with self._lock:
            paths = project.setdefault("paths", {})
            paths[kind] = str(target)
            self._touch(project)
            self._save(f"link {kind} of {project.get('name')}")
        return project

    def set_focus(self, project: dict[str, Any], text: str) -> dict[str, Any]:
        with self._lock:
            project["current_focus"] = " ".join(text.split())
            self._touch(project)
            self._save(f"focus of {project.get('name')}")
        return project

    def add_decision(self, project: dict[str, Any], text: str, *, source: str = "user") -> dict[str, Any]:
        clean = " ".join(text.split())
        if not clean:
            raise ValueError("The decision is empty")
        decisions = project.setdefault("decisions", [])
        entry = {"id": f"{project.get('id')}.{_slug(clean)[:40]}-{len(decisions) + 1}", "decision": clean, "status": "active", "made_at": _today(), "source": source}
        with self._lock:
            decisions.append(entry)
            self._touch(project)
            self._save(f"decision for {project.get('name')}")
        return entry

    def add_task(self, project: dict[str, Any], text: str, *, source: str = "user") -> dict[str, Any]:
        clean = " ".join(text.split())
        if not clean:
            raise ValueError("The task is empty")
        tasks = project.setdefault("next_actions", [])
        next_id = max((int(item.get("id", 0)) for item in tasks if isinstance(item, dict) and str(item.get("id", "")).isdigit()), default=0) + 1
        entry = {"id": next_id, "text": clean, "status": "open", "created_at": _today(), "done_at": None, "source": source}
        with self._lock:
            tasks.append(entry)
            self._touch(project)
            self._save(f"task for {project.get('name')}")
        return entry

    def complete_task(self, project: dict[str, Any], task_id: int) -> dict[str, Any]:
        for item in project.get("next_actions", []):
            if isinstance(item, dict) and int(item.get("id", -1)) == int(task_id):
                with self._lock:
                    item["status"] = "done"
                    item["done_at"] = _today()
                    self._touch(project)
                    self._save(f"task done for {project.get('name')}")
                return item
        raise ValueError(f"No task {task_id} in {project.get('name')}")

    def open_tasks(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in project.get("next_actions", []) if isinstance(item, dict) and item.get("status", "open") == "open"]

    def active_decisions(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in project.get("decisions", []) if isinstance(item, dict) and item.get("status", "active") == "active" and item.get("decision")]

    def touch(self, project: dict[str, Any]) -> None:
        with self._lock:
            self._touch(project)
            self._save(f"opened {project.get('name')}")

    def _touch(self, project: dict[str, Any]) -> None:
        metadata = project.setdefault("metadata", {})
        metadata["updated_at"] = _today()
        metadata["last_opened_at"] = _today()

    def project_for_path(self, path: str | Path) -> dict[str, Any] | None:
        candidate = Path(path).expanduser()
        for project in self.active_projects():
            paths = project.get("paths") or {}
            if any(_within(candidate, str(paths.get(kind) or "")) for kind in PATH_KINDS):
                return project
        return None

    def project_for_name(self, name: str) -> dict[str, Any] | None:
        needle = " ".join(name.split()).casefold()
        if not needle:
            return None
        for project in self.active_projects():
            label = str(project.get("name", "")).casefold()
            if label and (label in needle or needle in label):
                return project
        return None

    def infer(self) -> tuple[dict[str, Any] | None, str]:
        context = self.context_service
        current = None
        if context is not None and not getattr(context, "paused", False):
            try:
                current = context.current()
            except Exception:
                current = None
        if current is None:
            return None, "nothing is in view"
        if current.target:
            project = self.project_for_path(current.target)
            if project is not None:
                return project, f"{current.target} is inside its linked folders"
        workspace = None
        if self.code_service is not None:
            try:
                workspace = self.code_service.active_workspace()
            except Exception:
                workspace = None
        if workspace is not None:
            project = self.project_for_path(workspace.root)
            if project is not None:
                return project, f"the AL workspace at {workspace.root} is linked to it"
            for label in (workspace.name, workspace.root.name):
                project = self.project_for_name(label)
                if project is not None:
                    return project, f"the AL workspace {label} matches its name"
        if current.project:
            project = self.project_for_name(str(current.project))
            if project is not None:
                return project, f"the window title names {current.project}"
        return None, f"{current.describe()} matches no project"

    def describe(self, project: dict[str, Any]) -> str:
        lines = [f"{project.get('name')} ({project.get('id')}, {project.get('status', 'active')})"]
        if project.get("summary"):
            lines.append(str(project["summary"]))
        if project.get("current_focus"):
            lines.append(f"Focus: {project['current_focus']}")
        paths = project.get("paths") or {}
        linked = [f"{kind} {paths[kind]}" for kind in PATH_KINDS if paths.get(kind)]
        if linked:
            lines.append("Linked: " + "; ".join(linked))
        if project.get("technologies"):
            lines.append("Technologies: " + ", ".join(str(item) for item in project["technologies"]))
        decisions = self.active_decisions(project)
        if decisions:
            lines.append("Decisions: " + "; ".join(str(item["decision"]) for item in decisions[:6]))
        tasks = self.open_tasks(project)
        if tasks:
            lines.append("Open tasks: " + "; ".join(f"{item['id']}. {item['text']}" for item in tasks[:8]))
        last = (project.get("metadata") or {}).get("last_opened_at")
        if last:
            lines.append(f"Last opened {last}")
        return "\n".join(lines)


__all__ = ["PATH_KINDS", "PROJECTS_FILE", "ProjectService"]
