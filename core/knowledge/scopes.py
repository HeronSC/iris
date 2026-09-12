# File: core/knowledge/scopes.py

from __future__ import annotations

GLOBAL = "global"
PROJECT_PREFIX = "project:"
SESSION_PREFIX = "session:"

SCOPE_WORDS = ("global", "project", "session")


def project_scope(project_id: str | None) -> str | None:
    clean = str(project_id or "").strip()
    return f"{PROJECT_PREFIX}{clean}" if clean else None


def session_scope(session_id: str | None) -> str | None:
    clean = str(session_id or "").strip()
    return f"{SESSION_PREFIX}{clean}" if clean else None


def visible_scopes(project_id: str | None = None, session_id: str | None = None) -> tuple[str, ...]:
    scopes = [GLOBAL]
    for scope in (project_scope(project_id), session_scope(session_id)):
        if scope and scope not in scopes:
            scopes.append(scope)
    return tuple(scopes)


def resolve_scope(word: str | None, *, project_id: str | None = None, session_id: str | None = None) -> str:
    choice = str(word or "global").strip().lower()
    if choice in {"", "global"}:
        return GLOBAL
    if choice == "project":
        scope = project_scope(project_id)
        if scope is None:
            raise ValueError("No project is active, so nothing can be scoped to one; /project <name> first")
        return scope
    if choice == "session":
        scope = session_scope(session_id)
        if scope is None:
            raise ValueError("No session is active, so nothing can be scoped to one")
        return scope
    if choice.startswith(PROJECT_PREFIX) or choice.startswith(SESSION_PREFIX):
        return choice
    raise ValueError(f"Unknown scope {word!r}; one of global, project, session")


def describe_scope(scope: str | None) -> str:
    value = str(scope or GLOBAL)
    if value == GLOBAL:
        return "global"
    if value.startswith(PROJECT_PREFIX):
        return f"project {value[len(PROJECT_PREFIX):]}"
    if value.startswith(SESSION_PREFIX):
        return f"session {value[len(SESSION_PREFIX):][:8]}"
    return value


def split_scope_flag(text: str) -> tuple[str | None, str]:
    stripped = (text or "").strip()
    if stripped.startswith("@"):
        head, _sep, rest = stripped.partition(" ")
        word = head[1:].lower()
        if word in SCOPE_WORDS:
            return word, rest.strip()
    return None, stripped


__all__ = ["GLOBAL", "PROJECT_PREFIX", "SCOPE_WORDS", "SESSION_PREFIX", "describe_scope", "project_scope", "resolve_scope", "session_scope", "split_scope_flag", "visible_scopes"]
