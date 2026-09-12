# File: core/actions/implementations/git_tools.py

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.actions.executor import path_is_allowed
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.results.models import Result, Source, status, table
from core.tools.models import PermissionLevel, ToolDefinition

BLAME_LINE = re.compile(r"^(?P<hash>\^?[0-9a-f]{7,40})\s+(?:\S+\s+)?\((?P<author>.+?)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<line>\d+)\)\s?(?P<text>.*)$")


def git_root(path: Path, *, git: str | None = None) -> Path | None:
    executable = git or shutil.which("git")
    if not executable:
        return None
    folder = path if path.is_dir() else path.parent
    try:
        completed = subprocess.run([executable, "-C", str(folder), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace", check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip())


class BlameArguments(BaseModel):
    path: str = Field(description="Full path of a file inside a git repository")
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1, description="Last line to include; 60 lines from the start when omitted")


class GitBlameAction:

    name = "git_blame"
    definition = ToolDefinition(
        name="git_blame",
        description="Who last changed each line of a file and when, for a line range, from git blame. Read-only.",
        arguments=BlameArguments,
        permission=PermissionLevel.READ,
        cost="seconds",
        keywords=("blame", "who changed", "who wrote", "last touched", "when was this line changed", "git blame"),
    )

    def __init__(self, git: str | None = None) -> None:
        self.git = git or shutil.which("git")

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = BlameArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if not self.git:
            return ValidationResult(ok=False, error="git is not installed or not on PATH")
        path = Path(arguments.path.strip().strip('"')).expanduser()
        if not path.is_absolute():
            return ValidationResult(ok=False, error=f"{arguments.path} is not a full path")
        roots = list(getattr(context, "allowed_roots", None) or [])
        if roots and not path_is_allowed(path, roots):
            return ValidationResult(ok=False, error=f"{path} is outside the folders Iris may read")
        if not path.is_file():
            return ValidationResult(ok=False, error=f"{path} does not exist")
        root = git_root(path, git=self.git)
        if root is None:
            return ValidationResult(ok=False, error=f"{path.name} is not inside a git repository")
        end = arguments.end_line or arguments.start_line + 59
        if end < arguments.start_line:
            return ValidationResult(ok=False, error="end_line is before start_line")
        return ValidationResult(ok=True, resolved_target=str(path), resolved_arguments={"path": str(path), "start_line": arguments.start_line, "end_line": end, "root": str(root)})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        path = Path(str(request.arguments["path"]))
        root = Path(str(request.arguments["root"]))
        start = int(request.arguments["start_line"])
        end = int(request.arguments["end_line"])
        try:
            completed = subprocess.run([self.git, "-C", str(root), "blame", "-L", f"{start},{end}", "--date=short", "--", str(path)], capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace", check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return ActionResult(status="failed", message=f"git blame failed: {error}", action=self.name, error="blame_failed")
        if completed.returncode != 0:
            return ActionResult(status="failed", message=f"git blame failed: {completed.stderr.strip()[:300]}", action=self.name, error="blame_failed")
        rows: list[tuple[Any, ...]] = []
        for line in completed.stdout.splitlines():
            match = BLAME_LINE.match(line)
            if match:
                rows.append((int(match.group("line")), match.group("hash").lstrip("^")[:8], match.group("author").strip(), match.group("date"), match.group("text")[:160]))
        source = Source("git_blame", "tool", str(path))
        if not rows:
            message = f"git blame returned nothing for {path.name} lines {start}-{end}."
            return ActionResult(status="success", message=message, action=self.name, results=(status("warning", message, source=source),))
        authors: dict[str, int] = {}
        for row in rows:
            authors[row[2]] = authors.get(row[2], 0) + 1
        newest = max(row[3] for row in rows)
        heading = f"{path.name} lines {start}-{end}: " + ", ".join(f"{author} ({count})" for author, count in sorted(authors.items(), key=lambda item: -item[1])) + f"; newest change {newest}"
        lines = [heading] + [f"{row[0]:>5} {row[1]} {row[3]} {row[2][:18]:<18} {row[4]}" for row in rows[:80]]
        results: tuple[Result, ...] = (table(("line", "commit", "author", "date", "text"), rows, source=source, title=f"Blame for {path.name}"),)
        return ActionResult(status="success", message="\n".join(lines), action=self.name, resolved_target=str(path), results=results)


GIT_ACTIONS = (GitBlameAction,)

__all__ = ["GIT_ACTIONS", "GitBlameAction", "git_root"]
