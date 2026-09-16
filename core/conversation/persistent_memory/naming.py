# File: core/conversation/persistent_memory/naming.py

from __future__ import annotations

import re

from typing import Any

from core.assistant.request_kinds import is_code_request
from core.conversation.creations import derive_title, detect_kind, is_refusal

_DOMAINS = (
    (re.compile(r"\b(?:bc|business\s+central|al\s+code|al|codeunit|navision|dynamics\s+365)\b", re.IGNORECASE), "BC"),
    (re.compile(r"\bpython\b", re.IGNORECASE), "Python"),
    (re.compile(r"\bpowershell\b", re.IGNORECASE), "PowerShell"),
    (re.compile(r"\btypescript\b", re.IGNORECASE), "TypeScript"),
    (re.compile(r"\b(?:javascript|node\.?js)\b", re.IGNORECASE), "JavaScript"),
    (re.compile(r"(?:\bc#|\bcsharp\b|\.net\b)", re.IGNORECASE), "C#"),
    (re.compile(r"\b(?:sql|t-sql)\b", re.IGNORECASE), "SQL"),
    (re.compile(r"\b(?:excel|spreadsheet|vba)\b", re.IGNORECASE), "Excel"),
    (re.compile(r"\b(?:azure\s+devops|pipeline|pipelines)\b", re.IGNORECASE), "DevOps"),
)
_ARTIFACT = re.compile(r"\b(function|procedure|codeunit|script|query|class|method|report|page|table|extension|api|endpoint|regex|unit\s+test|module|snippet)s?\b", re.IGNORECASE)
_REQUEST_PREFIX = re.compile(
    r"^(?:(?:please|hey|hi|hello|ok|okay|so|iris|now|and)[,\s]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:help\s+me\s+(?:to\s+)?|i\s+(?:need|want|would\s+like|'?d\s+like)\s+(?:you\s+)?(?:to\s+)?|let'?s\s+)?"
    r"(?:write|create|make(?:\s+up)?|build|generate|draft|compose|give\s+me|show\s+me|tell\s+me\s+about|tell\s+me|explain|describe|"
    r"find|look\s+up|research|recommend|suggest|compare|list|get\s+me|what\s+is|what\s+are|what'?s|who\s+is|who\s+was|"
    r"how\s+do\s+i|how\s+to|how\s+can\s+i|do\s+you\s+know|is\s+there|are\s+there)?\s*",
    re.IGNORECASE,
)
_ARTICLES = re.compile(r"^(?:a|an|the|some|me|my|our|this|that|another|\d+[\s-]*words?)\s+", re.IGNORECASE)
_CONNECTORS = re.compile(r"^(?:for|that|to|which|about|on|of|with|in|regarding)\s+", re.IGNORECASE)
_SMALL_WORDS = {"a", "an", "the", "and", "or", "of", "for", "with", "in", "on", "to", "at", "by", "vs", "from", "as", "into"}
_MAX_WORDS = 7
_SYSTEM_PROMPT = (
    "You name conversation topics for a personal assistant's memory. Reply with the name only: no quotes, no explanation, no trailing period."
)


def _title_case(text: str) -> str:
    words = [word for word in re.split(r"\s+", (text or "").strip()) if word]
    out: list[str] = []
    for index, word in enumerate(words[:_MAX_WORDS]):
        lowered = word.lower()
        if index > 0 and lowered in _SMALL_WORDS:
            out.append(lowered)
        elif word.isupper() and len(word) > 1:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def _strip_request_framing(message: str) -> str:
    text = re.sub(r"\s+", " ", (message or "").strip()).strip(" .!?")
    text = re.split(r"[.!?;:]\s", text, maxsplit=1)[0]
    text = _REQUEST_PREFIX.sub("", text, count=1)
    text = _ARTICLES.sub("", text, count=1)
    return text.strip(" .!?,")


def derive_topic_title(user_message: str, assistant_message: str) -> str:
    message = (user_message or "").strip()
    kind = detect_kind(message, assistant_message or "") if len((assistant_message or "").strip()) >= 300 else None
    if kind is not None:
        title = derive_title(message, assistant_message, kind)
        return f"{kind.capitalize()}: {_title_case(title)}"

    domain = next((label for pattern, label in _DOMAINS if pattern.search(message)), None)
    remainder = _strip_request_framing(message)
    if domain is not None:
        for pattern, label in _DOMAINS:
            if label == domain:
                remainder = pattern.sub(" ", remainder)
        remainder = _ARTICLES.sub("", re.sub(r"\s+", " ", remainder).strip(), count=1)

    artifact = None
    if is_code_request(message) or domain is not None:
        found = _ARTIFACT.search(remainder)
        if found is not None:
            artifact = found.group(1).lower()
            remainder = (remainder[: found.start()] + " " + remainder[found.end():]).strip()
    remainder = _CONNECTORS.sub("", re.sub(r"\s+", " ", remainder).strip(), count=1)
    remainder = _ARTICLES.sub("", remainder, count=1).strip(" .!?,")
    remainder = re.sub(r"\s+(?:for|that|to|which|about|on|of|with|in|regarding|and|a|an|the)$", "", remainder, flags=re.IGNORECASE)

    subject = _title_case(remainder) if remainder else ""
    if artifact is not None and artifact not in subject.lower():
        subject = f"{subject} {_title_case(artifact)}".strip()
    if not subject:
        subject = _title_case(message) or "General"
    if domain is not None:
        return f"{domain}: {subject}"
    if is_code_request(message):
        return f"Code: {subject}"
    return subject


def naming_prompt(user_message: str, assistant_message: str) -> tuple[str, str]:
    prompt = (
        "Name the topic of this exchange in at most six words. Use the form '<Area>: <Subject>' when there is a clear area, "
        "for example 'BC: Sales Header Validation', 'Story: The Lighthouse Keeper', 'Python: Log File Parser', 'Home: Water Heater Replacement'. "
        "Never copy those examples; name only what is in the exchange below. "
        "If the assistant wrote a story, letter, poem, or document, use its title as the subject. "
        "Name what the exchange is about, never how it was asked.\n\n"
        f"User request:\n{(user_message or '').strip()[:1500]}\n\n"
        f"Assistant response:\n{(assistant_message or '').strip()[:2500]}"
    )
    return _SYSTEM_PROMPT, prompt


def select_naming_exchange(messages: list[dict[str, Any]]) -> tuple[str, str]:
    ordered = sorted(messages, key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)))
    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for message in ordered:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role == "user":
            pending_user = content
        elif role == "assistant" and pending_user is not None:
            pairs.append((pending_user, content))
            pending_user = None
    if not pairs:
        first_user = next((str(item.get("content") or "") for item in ordered if item.get("role") == "user"), "")
        return first_user, ""
    substantive = [pair for pair in pairs if not is_refusal(pair[1]) and not _looks_like_tool_call(pair[1])]
    if not substantive:
        return pairs[0]
    return substantive[0]


def _looks_like_tool_call(text: str) -> bool:
    stripped = (text or "").strip().lstrip("`").lstrip("json").strip()
    return stripped.startswith("{") and '"name"' in stripped[:200] and '"arguments"' in stripped


def parse_model_title(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    first = text.splitlines()[0].strip().strip("\"'“”`*_ .")
    first = re.sub(r"^(?:topic|name|title)\s*:\s*", "", first, flags=re.IGNORECASE).strip()
    if not first or len(first) > 80 or len(first.split()) > 8:
        return None
    return first


__all__ = ["derive_topic_title", "naming_prompt", "parse_model_title", "select_naming_exchange"]
