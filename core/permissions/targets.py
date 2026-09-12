# File: core/permissions/targets.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

PATH_ARGUMENT_KEYS = (
    "path",
    "root",
    "file_path",
    "folder",
    "directory",
    "destination",
    "executable",
    "repo_path",
    "target_path",
)

URL_ARGUMENT_KEYS = ("url", "href", "endpoint", "link", "address")

_URL_SCHEMES = ("http://", "https://", "ftp://", "ws://", "wss://")


def looks_like_url(value: str) -> bool:
    return value.strip().lower().startswith(_URL_SCHEMES)


def looks_like_path(value: str) -> bool:
    text = value.strip()
    if not text or looks_like_url(text):
        return False
    if text.startswith(("\\\\", "/", "~")):
        return True
    if len(text) > 2 and text[1] == ":" and text[2] in "\\/":
        return True
    return "\\" in text or "/" in text


def paths_in(arguments: dict[str, Any] | None, target: str | None = None) -> tuple[str, ...]:
    found: list[str] = []
    for key, value in (arguments or {}).items():
        if not isinstance(value, str) or not value.strip():
            continue
        if key in PATH_ARGUMENT_KEYS or (key.endswith("_path") and not looks_like_url(value)):
            found.append(value.strip())
        elif looks_like_path(value):
            found.append(value.strip())
    if target and looks_like_path(target):
        found.append(target.strip())
    return tuple(dict.fromkeys(found))


def hosts_in(arguments: dict[str, Any] | None, target: str | None = None) -> tuple[str, ...]:
    found: list[str] = []
    for key, value in (arguments or {}).items():
        if not isinstance(value, str) or not value.strip():
            continue
        if key in URL_ARGUMENT_KEYS or looks_like_url(value):
            host = host_of(value)
            if host:
                found.append(host)
    if target:
        host = host_of(target)
        if host:
            found.append(host)
    return tuple(dict.fromkeys(found))


def host_of(value: str) -> str | None:
    text = value.strip()
    if not looks_like_url(text):
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    return (parsed.hostname or "").lower() or None


def within_any(path: str, roots: Iterable[Path]) -> bool:
    try:
        candidate = Path(path).expanduser()
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            candidate.resolve().relative_to(Path(root).expanduser().resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    candidate = host.strip().lower().rstrip(".")
    for raw in patterns:
        pattern = str(raw).strip().lower().lstrip("*").lstrip(".")
        if not pattern:
            continue
        if candidate == pattern or candidate.endswith(f".{pattern}"):
            return True
    return False


__all__ = [
    "PATH_ARGUMENT_KEYS",
    "URL_ARGUMENT_KEYS",
    "host_matches",
    "host_of",
    "hosts_in",
    "looks_like_path",
    "looks_like_url",
    "paths_in",
    "within_any",
]
