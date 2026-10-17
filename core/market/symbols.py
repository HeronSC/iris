# File: core/market/symbols.py

from __future__ import annotations


def normalize_symbol(raw: object) -> str | None:
    text = str(raw or "").strip().upper().replace(".", "-")
    if not text:
        return None
    if text in {"BRK-B", "BF-B"}:
        return text
    if not text.isalpha() or not 1 <= len(text) <= 5:
        return None
    return text
