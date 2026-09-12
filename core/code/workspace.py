# File: core/code/workspace.py

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

PACKAGE_NAME = re.compile(r"^(?P<publisher>[^_]+)_(?P<name>.+)_(?P<version>\d+(?:\.\d+){1,3})\.app$", re.IGNORECASE)
SKIP_FOLDERS = {".git", ".alpackages", ".snapshots", "node_modules", ".vscode", "__pycache__", "output", ".venv"}


@dataclass(frozen=True)
class PackageInfo:
    path: Path
    publisher: str
    name: str
    version: str

    @property
    def label(self) -> str:
        return f"{self.name} {self.version} ({self.publisher})"


@dataclass(frozen=True)
class LaunchConfig:
    name: str
    server: str | None = None
    environment_type: str | None = None
    environment_name: str | None = None
    tenant: str | None = None
    startup_object: str | None = None

    def describe(self) -> str:
        parts = [self.name]
        if self.server:
            parts.append(self.server)
        if self.environment_type or self.environment_name:
            parts.append(" ".join(item for item in (self.environment_type, self.environment_name) if item))
        if self.startup_object:
            parts.append(f"starts at {self.startup_object}")
        return ": ".join(parts[:2]) + ("" if len(parts) < 3 else ", " + ", ".join(parts[2:]))


@dataclass(frozen=True)
class ALWorkspace:
    root: Path
    app: dict[str, Any] = field(default_factory=dict)
    launch: tuple[LaunchConfig, ...] = ()
    packages: tuple[PackageInfo, ...] = ()

    @property
    def name(self) -> str:
        return str(self.app.get("name") or self.root.name)

    @property
    def publisher(self) -> str:
        return str(self.app.get("publisher") or "")

    @property
    def version(self) -> str:
        return str(self.app.get("version") or "")

    @property
    def dependencies(self) -> tuple[dict[str, Any], ...]:
        items = self.app.get("dependencies")
        return tuple(item for item in items if isinstance(item, dict)) if isinstance(items, list) else ()

    @property
    def id_ranges(self) -> tuple[tuple[int, int], ...]:
        ranges = self.app.get("idRanges")
        if not isinstance(ranges, list):
            single = self.app.get("idRange")
            ranges = [single] if isinstance(single, dict) else []
        found: list[tuple[int, int]] = []
        for item in ranges:
            if isinstance(item, dict) and "from" in item and "to" in item:
                try:
                    found.append((int(item["from"]), int(item["to"])))
                except (TypeError, ValueError):
                    continue
        return tuple(found)

    def source_files(self) -> list[Path]:
        found: list[Path] = []
        for path in self.root.rglob("*.al"):
            if any(part in SKIP_FOLDERS for part in path.relative_to(self.root).parts[:-1]):
                continue
            found.append(path)
        return sorted(found)

    def find_source(self, file_name: str) -> Path | None:
        wanted = file_name.strip().lower()
        if not wanted:
            return None
        for path in self.source_files():
            if path.name.lower() == wanted:
                return path
        return None

    def describe(self) -> str:
        lines = [f"{self.name} {self.version} by {self.publisher or 'unknown publisher'} at {self.root}"]
        platform = self.app.get("platform")
        application = self.app.get("application")
        runtime = self.app.get("runtime")
        targets = ", ".join(item for item in (application and f"application {application}", platform and f"platform {platform}", runtime and f"runtime {runtime}") if item)
        if targets:
            lines.append(f"Targets {targets}")
        if self.id_ranges:
            lines.append("Object ids " + ", ".join(f"{low}-{high}" for low, high in self.id_ranges))
        if self.dependencies:
            lines.append("Depends on " + ", ".join(f"{item.get('name')} {item.get('version', '')}".strip() for item in self.dependencies))
        if self.launch:
            lines.append("Launch: " + "; ".join(item.describe() for item in self.launch))
        if self.packages:
            lines.append(f"{len(self.packages)} symbol packages in .alpackages: " + ", ".join(item.label for item in self.packages[:8]) + (" …" if len(self.packages) > 8 else ""))
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "name": self.name,
            "publisher": self.publisher,
            "version": self.version,
            "platform": self.app.get("platform"),
            "application": self.app.get("application"),
            "runtime": self.app.get("runtime"),
            "id_ranges": [list(item) for item in self.id_ranges],
            "dependencies": [dict(item) for item in self.dependencies],
            "launch": [item.__dict__ for item in self.launch],
            "packages": [item.label for item in self.packages],
        }


JSONC_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)
JSONC_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def parse_jsonc(text: str) -> Any:
    cleaned = JSONC_COMMENT.sub("", text)
    cleaned = JSONC_TRAILING_COMMA.sub(r"\1", cleaned)
    return json.loads(cleaned)


def _read_json(path: Path) -> Any:
    try:
        return parse_jsonc(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        logger.warning("Could not read %s: %s", path, error)
        return None


def find_workspace_root(start: str | Path) -> Path | None:
    candidate = Path(start).expanduser()
    if candidate.is_file():
        candidate = candidate.parent
    for folder in (candidate, *candidate.parents):
        if (folder / "app.json").is_file():
            return folder
    return None


def parse_package_name(path: Path) -> PackageInfo:
    match = PACKAGE_NAME.match(path.name)
    if match:
        return PackageInfo(path=path, publisher=match.group("publisher"), name=match.group("name"), version=match.group("version"))
    return PackageInfo(path=path, publisher="", name=path.stem, version="")


def load_workspace(root: str | Path) -> ALWorkspace | None:
    folder = find_workspace_root(root)
    if folder is None:
        return None
    app = _read_json(folder / "app.json")
    if not isinstance(app, dict):
        app = {}
    launch: list[LaunchConfig] = []
    launch_payload = _read_json(folder / ".vscode" / "launch.json") if (folder / ".vscode" / "launch.json").is_file() else None
    if isinstance(launch_payload, dict):
        for item in launch_payload.get("configurations") or []:
            if not isinstance(item, dict) or item.get("type") != "al":
                continue
            startup = None
            if item.get("startupObjectType") and item.get("startupObjectId"):
                startup = f"{item['startupObjectType']} {item['startupObjectId']}"
            launch.append(
                LaunchConfig(
                    name=str(item.get("name") or "AL"),
                    server=item.get("server"),
                    environment_type=item.get("environmentType"),
                    environment_name=item.get("environmentName"),
                    tenant=item.get("tenant"),
                    startup_object=startup,
                )
            )
    packages_dir = folder / ".alpackages"
    packages = tuple(sorted((parse_package_name(path) for path in packages_dir.glob("*.app")), key=lambda item: (item.name.lower(), item.version))) if packages_dir.is_dir() else ()
    return ALWorkspace(root=folder, app=app, launch=tuple(launch), packages=packages)


def find_workspaces(roots: Iterable[str | Path], *, max_depth: int = 3) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(base, 0)]
        while stack:
            folder, depth = stack.pop()
            if (folder / "app.json").is_file():
                key = str(folder).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(folder)
                continue
            if depth >= max_depth:
                continue
            try:
                children = [child for child in folder.iterdir() if child.is_dir() and child.name not in SKIP_FOLDERS and not child.name.startswith(".")]
            except OSError:
                continue
            stack.extend((child, depth + 1) for child in children)
    return sorted(found)


def find_workspace_by_name(name: str, roots: Iterable[str | Path], *, max_depth: int = 3) -> Path | None:
    wanted = name.strip().lower()
    if not wanted:
        return None
    for folder in find_workspaces(roots, max_depth=max_depth):
        if folder.name.lower() == wanted:
            return folder
    for folder in find_workspaces(roots, max_depth=max_depth):
        app = _read_json(folder / "app.json")
        if isinstance(app, dict) and str(app.get("name") or "").strip().lower() == wanted:
            return folder
    return None


__all__ = [
    "ALWorkspace",
    "LaunchConfig",
    "PackageInfo",
    "find_workspace_by_name",
    "parse_jsonc",
    "find_workspace_root",
    "find_workspaces",
    "load_workspace",
    "parse_package_name",
]
