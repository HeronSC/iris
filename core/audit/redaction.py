# File: core/audit/redaction.py

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

SECRET_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "privatekey",
    "accesskey",
    "clientsecret",
    "sessionkey",
    "cookie",
)

SECRET_KEY_EXCEPTIONS: frozenset[str] = frozenset(
    {"tokencount", "tokens", "prompttokens", "completiontokens", "maxtokens", "tokenizer"}
)

MAX_DEPTH = 8

_SEPARATORS = re.compile(r"[\s_\-.]+")
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)(?P<user>[^/:@\s]+):(?P<secret>[^/@\s]+)@")
_BEARER = re.compile(r"\b(?P<prefix>bearer|basic|token)\s+(?P<secret>[A-Za-z0-9._~+/=\-]{8,})", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"(?P<key>\b(?:api[_-]?key|access[_-]?token|auth|token|password|secret)\b\s*[=:]\s*)(?P<secret>[^&\s,;\"']{4,})",
    re.IGNORECASE,
)


def normalize_key(name: object) -> str:
    return _SEPARATORS.sub("", str(name)).lower()


def is_secret_key(name: object) -> bool:
    normalized = normalize_key(name)
    if normalized in SECRET_KEY_EXCEPTIONS:
        return False
    return any(part in normalized for part in SECRET_KEY_PARTS)


def redact_text(text: str) -> str:
    redacted = _URL_CREDENTIALS.sub(lambda match: f"{match.group('scheme')}{match.group('user')}:{REDACTED}@", text)
    redacted = _BEARER.sub(lambda match: f"{match.group('prefix')} {REDACTED}", redacted)
    redacted = _QUERY_SECRET.sub(lambda match: f"{match.group('key')}{REDACTED}", redacted)
    return redacted


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: REDACTED if is_secret_key(key) else redact(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted = [redact(item, depth=depth + 1) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    if isinstance(value, str):
        return redact_text(value)
    return value


__all__ = ["REDACTED", "is_secret_key", "redact", "redact_text"]
