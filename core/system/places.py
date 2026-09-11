# File: core/system/places.py

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class KnownFolder:
    path: str
    source: str

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def label(self) -> str:
        return f"{self.path}  ({self.source})"


def _folder_from_uri(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return str(Path(path)) if path else None


def vscode_folders(user_dir: Path | None = None) -> list[KnownFolder]:
    base = user_dir or (Path(os.environ.get("APPDATA", "")) / "Code" / "User")
    found: dict[str, float] = {}
    for meta in (base / "workspaceStorage").glob("*/workspace.json"):
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
            stamp = meta.stat().st_mtime
        except (OSError, ValueError):
            continue
        folder = _folder_from_uri(str(payload.get("folder") or ""))
        if folder:
            found[folder] = max(found.get(folder, 0.0), stamp)
    storage = base / "globalStorage" / "storage.json"
    if storage.exists():
        try:
            payload = json.loads(storage.read_text(encoding="utf-8"))
            workspaces = (payload.get("profileAssociations") or {}).get("workspaces") or {}
        except (OSError, ValueError, AttributeError):
            workspaces = {}
        for uri in workspaces:
            folder = _folder_from_uri(str(uri))
            if folder:
                found.setdefault(folder, 0.0)
    ordered = sorted(found.items(), key=lambda item: -item[1])
    return [KnownFolder(path=path, source="vscode") for path, _ in ordered if Path(path).exists()]


def root_subfolders(roots: Iterable[Path], *, depth: int = 2, limit: int = 4000) -> list[KnownFolder]:
    found: list[KnownFolder] = []
    for root in roots:
        try:
            base = Path(root)
            if not base.is_dir():
                continue
        except OSError:
            continue
        stack: list[tuple[Path, int]] = [(base, 0)]
        while stack and len(found) < limit:
            current, level = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if not entry.is_dir(follow_symlinks=False) or entry.name.startswith((".", "__", "$")):
                            continue
                        found.append(KnownFolder(path=entry.path, source="documents"))
                        if level + 1 < depth:
                            stack.append((Path(entry.path), level + 1))
            except OSError:
                continue
    return found


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def find_folders(query: str, candidates: Iterable[KnownFolder], *, limit: int = 5) -> list[KnownFolder]:
    wanted = _normalize(query)
    if not wanted:
        return []
    wanted_tokens = set(wanted.split())
    scored: list[tuple[float, KnownFolder]] = []
    seen: set[str] = set()
    for folder in candidates:
        key = folder.path.lower()
        if key in seen:
            continue
        seen.add(key)
        name = _normalize(folder.name)
        if not name:
            continue
        if name == wanted:
            score = 100.0
        elif name.startswith(wanted) or wanted.startswith(name):
            score = 80.0
        elif wanted_tokens and wanted_tokens <= set(name.split()):
            score = 70.0
        else:
            ratio = difflib.SequenceMatcher(None, wanted, name).ratio()
            overlap = len(wanted_tokens & set(name.split()))
            score = 40.0 + 20.0 * ratio if ratio >= 0.7 else (25.0 * overlap / len(wanted_tokens) if overlap else 0.0)
        if score > 0:
            scored.append((score + (1.0 if folder.source == "vscode" else 0.0), folder))
    scored.sort(key=lambda pair: (-pair[0], pair[1].path.lower()))
    return [folder for _, folder in scored[: max(1, limit)]]
