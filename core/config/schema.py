# File: core/config/schema.py

from __future__ import annotations

from typing import Any

KNOWN_KEYS: dict[str, tuple[type, ...]] = {
    "assistant_name": (str,),
    "assistant_user": (str,),
    "memory_path": (str,),
    "session_path": (str,),
    "proposal_path": (str,),
    "audit_path": (str,),
    "action_audit_path": (str,),
    "metrics_path": (str,),
    "model": (str,),
    "models": (dict,),
    "llm_server": (str,),
    "llm_timeout_seconds": (int, float, str),
    "llm": (dict,),
    "image_server": (str,),
    "conversation": (dict,),
    "memory": (dict,),
    "memory_updates": (dict,),
    "general_knowledge": (dict,),
    "document_search": (dict,),
    "applications": (dict, list),
    "web_shortcuts": (dict,),
    "knowledge": (dict,),
    "notifications": (dict,),
    "mcp_servers": (dict,),
    "web": (dict,),
    "permissions": (dict,),
    "http": (dict,),
    "latency_budget_ms": (dict,),
    "backups": (dict,),
    "blue_iris": (dict,),
    "code": (dict,),
    "context": (dict,),
    "retention": (dict,),
    "ui": (dict,),
}

SECRET_LIKE = ("password", "secret", "token", "api_key", "apikey", "client_secret")


def check_config(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for key, value in config.items():
        expected = KNOWN_KEYS.get(str(key))
        if expected is None:
            warnings.append(f"Unknown setting '{key}' is ignored")
            continue
        if not isinstance(value, expected):
            names = " or ".join(item.__name__ for item in expected)
            errors.append(f"'{key}' must be {names}, not {type(value).__name__}")
    for path, value in _walk(config):
        leaf = path.rsplit(".", 1)[-1].lower()
        if any(word in leaf for word in SECRET_LIKE) and isinstance(value, str) and value.strip() and not leaf.endswith("_secret"):
            warnings.append(f"'{path}' looks like a secret; keep it in Credential Manager (/secrets set) and out of config.json")
    return errors, warnings


def _walk(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.extend(_walk(value, path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk(value, f"{prefix}[{index}]"))
    else:
        found.append((prefix, node))
    return found


__all__ = ["KNOWN_KEYS", "SECRET_LIKE", "check_config"]
