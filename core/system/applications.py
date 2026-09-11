# File: core/system/applications.py

from __future__ import annotations

import difflib
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600.0
_SKIP_WORDS = frozenset({"uninstall", "readme", "help", "documentation", "website", "license", "update", "updater", "setup", "installer"})


@dataclass(frozen=True)
class InstalledApplication:
    name: str
    executable: str
    source: str

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.executable})"


def _start_menu_roots() -> list[Path]:
    roots: list[Path] = []
    for variable in ("ProgramData", "APPDATA"):
        base = os.environ.get(variable)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [root for root in roots if root.exists()]


def resolve_shortcut(path: Path) -> str | None:
    try:
        #! @allow-local-import
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        return str(shell.CreateShortcut(str(path)).TargetPath or "")
    except Exception:
        return None


def _shortcut_applications(resolve: Callable[[Path], str | None]) -> list[InstalledApplication]:
    found: list[InstalledApplication] = []
    for root in _start_menu_roots():
        for link in root.rglob("*.lnk"):
            stem = link.stem.strip()
            if not stem or any(word in stem.lower() for word in _SKIP_WORDS):
                continue
            target = resolve(link)
            if not target or not target.lower().endswith(".exe") or not Path(target).exists():
                continue
            found.append(InstalledApplication(name=stem, executable=target, source="start-menu"))
    return found


def _registry_applications() -> list[InstalledApplication]:
    try:
        #! @allow-local-import
        import winreg
    except ImportError:
        return []
    found: list[InstalledApplication] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths")
        except OSError:
            continue
        with key:
            count = winreg.QueryInfoKey(key)[0]
            for index in range(count):
                try:
                    name = winreg.EnumKey(key, index)
                    with winreg.OpenKey(key, name) as entry:
                        target = str(winreg.QueryValue(entry, None) or "").strip().strip('"')
                except OSError:
                    continue
                if target and Path(target).exists():
                    found.append(InstalledApplication(name=Path(name).stem, executable=target, source="app-paths"))
    return found


class ApplicationCatalog:
    def __init__(self, resolve: Callable[[Path], str | None] | None = None, *, include_registry: bool = True, cache_seconds: float = CACHE_SECONDS) -> None:
        self.resolve = resolve or resolve_shortcut
        self.include_registry = include_registry
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._items: list[InstalledApplication] | None = None
        self._scanned_at = 0.0

    def applications(self, refresh: bool = False) -> list[InstalledApplication]:
        with self._lock:
            stale = self._items is None or refresh or (time.monotonic() - self._scanned_at) > self.cache_seconds
            if stale:
                started = time.perf_counter()
                items = _shortcut_applications(self.resolve)
                if self.include_registry:
                    items.extend(_registry_applications())
                self._items = _dedupe(items)
                self._scanned_at = time.monotonic()
                logger.info("Scanned %d installed applications in %.0f ms", len(self._items), (time.perf_counter() - started) * 1000)
            return list(self._items or [])

    def find(self, query: str, *, limit: int = 5) -> list[InstalledApplication]:
        wanted = _normalize(query)
        if not wanted:
            return []
        scored: list[tuple[float, InstalledApplication]] = []
        for item in self.applications():
            score = _score(wanted, item)
            if score > 0:
                scored.append((score, item))
        on_path = shutil.which(query.strip())
        if on_path and on_path.lower().endswith(".exe") and all(on_path.lower() != item.executable.lower() for _, item in scored):
            scored.append((90.0, InstalledApplication(name=Path(on_path).stem, executable=on_path, source="path")))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name.lower()))
        return [item for _, item in scored[: max(1, limit)]]


def _normalize(text: str) -> str:
    lowered = (text or "").lower().replace("++", " plus plus").replace("+", " plus").replace("#", " sharp")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9. ]+", " ", lowered)).strip()


def _tokens(text: str) -> set[str]:
    return {token for token in _normalize(text).split() if token}


def _score(wanted: str, item: InstalledApplication) -> float:
    name = _normalize(item.name)
    stem = _normalize(Path(item.executable).stem)
    if wanted in {name, stem}:
        return 100.0
    if name.startswith(wanted) or stem.startswith(wanted):
        return 80.0
    query_tokens = _tokens(wanted)
    name_tokens = _tokens(item.name) | _tokens(Path(item.executable).stem)
    if query_tokens and query_tokens <= name_tokens:
        return 70.0
    ratio = max(difflib.SequenceMatcher(None, wanted, name).ratio(), difflib.SequenceMatcher(None, wanted, stem).ratio())
    if ratio >= 0.7:
        return 40.0 + 20.0 * ratio
    overlap = len(query_tokens & name_tokens)
    if query_tokens and overlap:
        return 30.0 * overlap / len(query_tokens)
    return 0.0


def _dedupe(items: list[InstalledApplication]) -> list[InstalledApplication]:
    seen: dict[str, InstalledApplication] = {}
    for item in items:
        key = item.executable.lower()
        current = seen.get(key)
        if current is None or (current.source == "app-paths" and item.source == "start-menu"):
            seen[key] = item
    return sorted(seen.values(), key=lambda item: item.name.lower())
