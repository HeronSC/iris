# File: core/voice/text.py

from __future__ import annotations

import re

FENCE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`]*)`")
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
URL = re.compile(r"https?://\S+")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
QUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
WHITESPACE = re.compile(r"\s+")


def speech_text(text: str, limit: int = 600) -> str:
    cleaned = FENCE.sub(" code omitted. ", text or "")
    cleaned = TABLE_ROW.sub(" ", cleaned)
    cleaned = INLINE_CODE.sub(r"\1", cleaned)
    cleaned = LINK.sub(r"\1", cleaned)
    cleaned = URL.sub("link", cleaned)
    cleaned = HEADING.sub("", cleaned)
    cleaned = QUOTE.sub("", cleaned)
    cleaned = BULLET.sub("", cleaned)
    cleaned = EMPHASIS.sub("", cleaned)
    cleaned = WHITESPACE.sub(" ", cleaned).strip()
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    window = cleaned[:limit]
    ends = [match.end() for match in SENTENCE_END.finditer(window)]
    if ends:
        return window[: ends[-1]].strip()
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).strip()
