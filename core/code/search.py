# File: core/code/search.py

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RESULTS = 40
DEFAULT_EXCLUDES = ("!.git/", "!.alpackages/", "!node_modules/", "!.snapshots/", "!__pycache__/", "!*.app", "!*.zip")


class SearchToolMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class Match:
    file: str
    line: int
    text: str


@dataclass(frozen=True)
class SearchOutcome:
    pattern: str
    root: Path
    matches: tuple[Match, ...]
    truncated: bool = False
    files_searched: int | None = None

    @property
    def files(self) -> list[str]:
        seen: list[str] = []
        for match in self.matches:
            if match.file not in seen:
                seen.append(match.file)
        return seen


class RipgrepSearch:
    def __init__(self, executable: str | None = None, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.executable = shutil.which("rg") if executable is None else (executable or None)
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def search(
        self,
        pattern: str,
        root: str | Path,
        *,
        glob: str | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> SearchOutcome:
        if not self.executable:
            raise SearchToolMissing("ripgrep (rg) is not installed or not on PATH")
        folder = Path(root)
        limit = max(1, int(max_results))
        command = [self.executable, "--json", "--line-number", "--no-messages", "--max-columns", "300", "--max-count", str(limit)]
        command.append("--case-sensitive" if case_sensitive else "--ignore-case")
        if not regex:
            command.append("--fixed-strings")
        for exclude in DEFAULT_EXCLUDES:
            command.extend(["--glob", exclude])
        if glob:
            command.extend(["--glob", glob])
        command.extend(["--", pattern, str(folder)])
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_seconds, check=False)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"Search took longer than {self.timeout_seconds:.0f} s") from error
        if completed.returncode not in (0, 1):
            raise RuntimeError(f"ripgrep failed ({completed.returncode}): {completed.stderr.strip()[:300]}")
        matches: list[Match] = []
        truncated = False
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_text = (data.get("path") or {}).get("text") or ""
            text = ((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
            try:
                relative = str(Path(path_text).relative_to(folder))
            except ValueError:
                relative = path_text
            if len(matches) >= limit:
                truncated = True
                break
            matches.append(Match(file=relative, line=int(data.get("line_number") or 0), text=text))
        return SearchOutcome(pattern=pattern, root=folder, matches=tuple(matches), truncated=truncated)


__all__ = ["DEFAULT_EXCLUDES", "DEFAULT_MAX_RESULTS", "Match", "RipgrepSearch", "SearchOutcome", "SearchToolMissing"]
