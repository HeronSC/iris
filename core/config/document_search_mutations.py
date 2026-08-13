from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from core.actions.models import ConfirmationPreview
from core.documents.models import normalize_config_token, normalize_relative_path, normalize_root_key, root_entry_path, root_entry_to_json


def ensure_document_search_section(config: dict[str, Any]) -> dict[str, Any]:
    document_search = config.get("document_search", {})
    if not isinstance(document_search, dict):
        document_search = {}
    config["document_search"] = document_search
    return document_search


def ensure_root_entries(document_search: dict[str, Any]) -> list[Any]:
    roots = document_search.get("roots", [])
    if not isinstance(roots, list):
        roots = []
    document_search["roots"] = roots
    return roots


def ensure_directory_groups(document_search: dict[str, Any]) -> dict[str, list[str]]:
    groups = document_search.get("directory_groups", {})
    if not isinstance(groups, dict):
        groups = {}
    document_search["directory_groups"] = groups
    return groups


def ensure_root_entry_object(root_entry: Any) -> dict[str, Any]:
    if isinstance(root_entry, dict):
        normalized = dict(root_entry)
    else:
        path_value = root_entry_path(root_entry)
        if path_value is None:
            raise ValueError("Root entry is missing a path")
        normalized = root_entry_to_json(path_value)

    normalized["path"] = str(normalized.get("path", "")).strip()
    for key in (
        "excluded_directory_groups",
        "excluded_directories",
        "excluded_directory_prefixes",
        "excluded_relative_paths",
    ):
        values = normalized.get(key, [])
        if not isinstance(values, list):
            values = []
        normalized[key] = values
    return normalized


def find_root_entry(roots: list[Any], root: Path | str) -> dict[str, Any] | None:
    root_key = normalize_root_key(root)
    for item in roots:
        path_value = root_entry_path(item)
        if path_value is None:
            continue
        if normalize_root_key(path_value) == root_key:
            return ensure_root_entry_object(item)
    return None


def upsert_root_entry(roots: list[Any], root: Path | str) -> dict[str, Any]:
    root_key = normalize_root_key(root)
    for index, item in enumerate(roots):
        path_value = root_entry_path(item)
        if path_value is None:
            continue
        if normalize_root_key(path_value) == root_key:
            normalized = ensure_root_entry_object(item)
            roots[index] = normalized
            return normalized
    normalized = root_entry_to_json(str(root))
    roots.append(normalized)
    return normalized


def remove_root_entry(roots: list[Any], root: Path | str) -> list[Any]:
    root_key = normalize_root_key(root)
    return [item for item in roots if (path_value := root_entry_path(item)) is None or normalize_root_key(path_value) != root_key]


def append_unique_text(values: list[str], candidate: str, *, normalize: Callable[[str], str] = normalize_config_token) -> bool:
    normalized_candidate = normalize(candidate)
    if not normalized_candidate:
        return False
    known = {normalize(str(item)) for item in values if normalize(str(item))}
    if normalized_candidate in known:
        return False
    values.append(candidate)
    return True


def append_unique_relative_path(values: list[str], candidate: str) -> bool:
    normalized_candidate = normalize_relative_path(candidate)
    known = {normalize_relative_path(str(item)) for item in values if str(item).strip()}
    if normalized_candidate in known:
        return False
    values.append(candidate)
    return True


def render_json_preview(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def build_confirmation_preview(
    *,
    summary: str,
    target: str | None,
    after_payload: Any,
    impact: str | None = None,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ConfirmationPreview:
    return ConfirmationPreview(
        summary=summary,
        target=target,
        after=render_json_preview(after_payload),
        impact=impact,
        title=title,
        metadata=metadata or {},
    )