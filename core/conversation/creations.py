# File: core/conversation/creations.py

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

from core.assistant.request_kinds import is_code_request

logger = structlog.get_logger(__name__)

_KINDS = (
    r"short\s+story|story|tale|fable|chapter|novel|letter|e-?mail|poem|haiku|sonnet|limerick|essay|article|"
    r"blog\s+post|speech|toast|eulogy|memo|report|outline|script|screenplay|lyrics|song|recipe|proposal|"
    r"cover\s+letter|resume|bio|biography|announcement|newsletter|press\s+release|pitch|itinerary|agenda|"
    r"joke|riddle|plan|document"
)
_CREATE = re.compile(
    r"\b(?:write|draft|compose|create|make\s+up|generate|pen|craft|produce|prepare|tell\s+me|come\s+up\s+with|give\s+me)\b"
    r"(?:\s+[\w'-]+){0,8}?\s+(" + _KINDS + r")\b",
    re.IGNORECASE,
)
_CONTINUE = re.compile(
    r"\b(?:continue|extend|rewrite|revise|expand|finish|lengthen|shorten|polish|improve|add\s+to|keep\s+going\s+with)\b"
    r"(?:\s+[\w'-]+){0,6}?\s+(" + _KINDS + r")\b",
    re.IGNORECASE,
)
_REFUSAL = re.compile(r"^\s*(?:i(?:['’]m| am)\s+(?:unable|not\s+able|sorry)|i\s+can(?:\s*no|['’])t|i\s+won['’]?t|i\s+don['’]?t\s+have|i\s+(?:must|have\s+to)\s+decline|i\s+could\s*n(?:ot|['’]t)|i\s+was\s+(?:unable|not\s+able)|(?:sorry|unfortunately)\b)", re.IGNORECASE)
_HEADING = re.compile(r"^\s*(?:#{1,3}\s*)?[*_\"“]*([^*_\"“”\n]{3,80}?)[*_\"”]*\s*:?\s*$")
_ABOUT = re.compile(r"\b(?:about|on|regarding|titled|called|named)\s+(.{3,80}?)(?:[.!?]|$)", re.IGNORECASE)
_MIN_CHARS = 300
_MAX_PROMPT = 300


@dataclass(frozen=True)
class Creation:
    path: Path
    title: str
    kind: str


def is_refusal(response: str) -> bool:
    text = (response or "").lstrip()
    if not text:
        return True
    if text.startswith("[ASSISTANT_PROMPT]"):
        return True
    return bool(_REFUSAL.match(text))


def detect_kind(user_message: str, response: str) -> str | None:
    message = (user_message or "").strip()
    if not message or message.startswith("/") or is_code_request(message):
        return None
    if len((response or "").strip()) < _MIN_CHARS:
        return None
    if is_refusal(response):
        return None
    match = _CREATE.search(message) or _CONTINUE.search(message)
    if match is None:
        return None
    kind = re.sub(r"\s+", " ", match.group(1)).strip().lower()
    return {"short story": "story", "tale": "story", "fable": "story", "e-mail": "email"}.get(kind, kind)


def derive_title(user_message: str, response: str, kind: str) -> str:
    for line in (response or "").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        heading = _HEADING.match(candidate)
        if heading is not None and len(candidate.split()) <= 12 and not candidate.endswith((".", ",")):
            return heading.group(1).strip().strip("\"'“”*_")
        break
    about = _ABOUT.search(user_message or "")
    if about is not None:
        subject = about.group(1).strip().strip("\"'“”.")
        return (subject[:1].upper() + subject[1:])[:80]
    return f"{kind.capitalize()} {datetime.now().strftime('%Y-%m-%d %H%M')}"


def _slug(title: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:60].strip() or "untitled"


class CreationStore:
    def __init__(self, folder: str | Path, *, indexer: Callable[[Path], Any] | None = None) -> None:
        self.folder = Path(folder).expanduser()
        self.indexer = indexer

    def capture(self, *, user_message: str, response: str, session_id: str | None, selected_tool: str | None = None) -> Creation | None:
        if selected_tool in {"write_file", "edit_file"}:
            return None
        kind = detect_kind(user_message, response)
        if kind is None:
            return None
        title = derive_title(user_message, response, kind)
        return self.save(title=title, content=response, kind=kind, prompt=user_message, session_id=session_id)

    def save(self, *, title: str, content: str, kind: str, prompt: str, session_id: str | None) -> Creation:
        self.folder.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        stem = f"{now.strftime('%Y-%m-%d')} {_slug(title)}"
        path = self.folder / f"{stem}.md"
        counter = 2
        while path.exists():
            path = self.folder / f"{stem} ({counter}).md"
            counter += 1
        prompt_line = re.sub(r"\s+", " ", (prompt or "").strip())[:_MAX_PROMPT]
        header = [
            f"# {title}",
            "",
            f"Kind: {kind}",
            f"Session: {session_id or 'unknown'}",
            f"Created: {now.replace(microsecond=0).isoformat()}",
            f"Prompt: {prompt_line}",
            "",
            "---",
            "",
        ]
        path.write_text("\n".join(header) + content.strip() + "\n", encoding="utf-8")
        if self.indexer is not None:
            try:
                self.indexer(path)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Creation not indexed", path=str(path), error=str(error))
        logger.info("Creation saved", path=str(path), kind=kind)
        return Creation(path=path, title=title, kind=kind)

    def search(self, words: list[str], *, limit: int = 5) -> list[dict[str, Any]]:
        if not self.folder.exists():
            return []
        wanted = [word.casefold() for word in words if word]
        hits: list[dict[str, Any]] = []
        for path in sorted(self.folder.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            haystack = content.casefold()
            if wanted and not all(word in haystack for word in wanted):
                continue
            first = content.splitlines()[0] if content else ""
            modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            hits.append({"path": str(path), "title": first.lstrip("# ").strip() or path.stem, "modified": modified, "text": content})
            if len(hits) >= limit:
                break
        return hits


__all__ = ["Creation", "CreationStore", "derive_title", "detect_kind", "is_refusal"]
