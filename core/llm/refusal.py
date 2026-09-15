# File: core/llm/refusal.py

from __future__ import annotations

import re

DECISION_CHARS = 160
REFUSAL_WINDOW = 400

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_OPEN_THINK = re.compile(r"<think>.*$", re.IGNORECASE | re.DOTALL)
_EMPHASIS = re.compile(r"[*_`#>]+")
_WHITESPACE = re.compile(r"\s+")

_PATTERNS = (
    r"i (?:can ?not|can'?t|won'?t|will not) (?:help|assist|comply|provide|create|write|generate|produce|continue|engage|participate|fulfil{1,2}|support|do that|do this)",
    r"i(?:'m| am) (?:not able|unable) to (?:help|assist|comply|provide|create|write|generate|produce|continue|engage|fulfil{1,2})",
    r"i must (?:decline|refuse)",
    r"i(?:'d| would) rather not",
    r"i(?:'m| am) not comfortable",
    r"i (?:do not|don'?t) feel comfortable",
    r"(?:as|being) an ai(?: language model)?[,\s]",
    r"as a language model",
    r"against my (?:guidelines|programming|principles|values)",
    r"(?:violates|goes against) my",
    r"not (?:something i can|appropriate for me)",
    r"i(?:'m| am) (?:designed|programmed) (?:to|not to)",
    r"i apologi[sz]e,? but i",
    r"sorry,? but i (?:can ?not|can'?t|won'?t|will not)",
)

_COMPILED = tuple(re.compile(pattern) for pattern in _PATTERNS)


def visible_text(text: str) -> str:
    body = _THINK_BLOCK.sub(" ", text or "")
    body = _OPEN_THINK.sub(" ", body)
    body = _EMPHASIS.sub(" ", body)
    body = body.replace("’", "'").replace("‘", "'")
    return _WHITESPACE.sub(" ", body).strip().lower()


def looks_like_refusal(text: str) -> bool:
    body = visible_text(text)
    if not body:
        return False
    window = body[:REFUSAL_WINDOW]
    return any(pattern.search(window) for pattern in _COMPILED)


def decided_enough(text: str) -> bool:
    return len(visible_text(text)) >= DECISION_CHARS


__all__ = ["DECISION_CHARS", "decided_enough", "looks_like_refusal", "visible_text"]
