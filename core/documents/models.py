from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def normalize_config_token(value: str) -> str:
    return str(value).strip().lower()


def normalize_relative_path(value: str) -> str:
    text = str(value).strip().replace("/", "\\")
    if not text:
        raise ValueError("relative path cannot be empty")
    candidate = Path(text)
    if candidate.is_absolute():
        raise ValueError("relative path must not be absolute")
    parts = [part.strip() for part in candidate.parts if part not in {"", "."}]
    if not parts:
        raise ValueError("relative path cannot be empty")
    if any(part == ".." for part in parts):
        raise ValueError("relative path must not contain '..'")
    return "/".join(part.lower() for part in parts)


def normalize_root_key(path: Path | str) -> str:
    candidate = path if isinstance(path, Path) else Path(str(path)).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    return str(resolved).lower()


@dataclass(frozen=True)
class DocumentSearchRoot:
    path: Path
    excluded_directory_groups: set[str] = field(default_factory=set)
    excluded_directories: set[str] = field(default_factory=set)
    excluded_directory_prefixes: set[str] = field(default_factory=set)
    excluded_relative_paths: set[str] = field(default_factory=set)


def root_entry_path(entry: Any) -> str | None:
    if isinstance(entry, str):
        text = entry.strip()
        return text or None
    if isinstance(entry, dict):
        text = str(entry.get("path", "")).strip()
        return text or None
    if isinstance(entry, DocumentSearchRoot):
        return str(entry.path)
    return None


def root_entry_to_json(path: Path | str) -> dict[str, Any]:
    return {
        "path": str(path),
        "excluded_directory_groups": [],
        "excluded_directories": [],
        "excluded_directory_prefixes": [],
        "excluded_relative_paths": [],
    }


def coerce_document_search_root(root: Path | DocumentSearchRoot) -> DocumentSearchRoot:
    if isinstance(root, DocumentSearchRoot):
        return root
    return DocumentSearchRoot(path=root)


@dataclass(frozen=True)
class DocumentSearchConfig:
    roots: list[Path | DocumentSearchRoot]
    excluded_directories: set[str]
    supported_extensions: set[str]
    max_file_size_mb: int
    excluded_directory_prefixes: set[str] = field(default_factory=set)
    directory_groups: dict[str, set[str]] = field(default_factory=dict)

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    def root_paths(self) -> list[Path]:
        return [coerce_document_search_root(root).path for root in self.roots]

    def effective_excluded_directories(self, root: Path | DocumentSearchRoot) -> set[str]:
        normalized_root = coerce_document_search_root(root)
        excluded = set(self.excluded_directories)
        for group_name in normalized_root.excluded_directory_groups:
            excluded.update(self.directory_groups.get(group_name, set()))
        excluded.update(normalized_root.excluded_directories)
        return excluded

    def effective_excluded_directory_prefixes(self, root: Path | DocumentSearchRoot) -> set[str]:
        normalized_root = coerce_document_search_root(root)
        prefixes = set(self.excluded_directory_prefixes)
        prefixes.update(normalized_root.excluded_directory_prefixes)
        return prefixes

    def is_excluded_directory(self, root: Path | DocumentSearchRoot, relative_path: Path, directory_name: str) -> bool:
        normalized_root = coerce_document_search_root(root)
        lowered = normalize_config_token(directory_name)
        if lowered in self.effective_excluded_directories(normalized_root):
            return True
        for prefix in self.effective_excluded_directory_prefixes(normalized_root):
            if prefix and lowered.startswith(prefix):
                return True

        normalized_relative = normalize_relative_path(str(relative_path))
        for excluded_path in normalized_root.excluded_relative_paths:
            if normalized_relative == excluded_path or normalized_relative.startswith(excluded_path + "/"):
                return True
        return False


@dataclass(frozen=True)
class FileRecord:
    id: str
    path: str
    name: str
    extension: str
    size: int
    created_at: str
    modified_at: str
    indexed_at: str
    content_hash: str
    content_status: str
    extracted_text: str
    extractor: str
    error: str | None


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    content_status: str
    extractor: str
    error: str | None = None


@dataclass(frozen=True)
class FileSearchQuery:
    text_terms: list[str]
    extensions: list[str]
    created_after: datetime | None
    created_before: datetime | None
    modified_after: datetime | None
    modified_before: datetime | None
    locations: list[Path]


@dataclass(frozen=True)
class RankedSearchResult:
    record: FileRecord
    score: float
    reasons: list[str]
