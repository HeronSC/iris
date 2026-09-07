from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.documents.models import DocumentSearchRoot, normalize_config_token, normalize_relative_path, normalize_root_key


class ConfigError(Exception):
    pass


class ConfigLoader:
    def __init__(self, config_path: str | Path | None = None) -> None:
        if config_path is None:
            self.config_path = Path(__file__).resolve().parents[1] / "config.json"
        else:
            self.config_path = Path(config_path)

    def load(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise ConfigError(f"Configuration file is missing: {self.config_path}")

        try:
            with self.config_path.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
        except json.JSONDecodeError as error:
            raise ConfigError(
                f"Invalid JSON in {self.config_path.name} at line {error.lineno}, column {error.colno}: {error.msg}"
            ) from error
        except OSError as error:
            raise ConfigError(f"Could not read {self.config_path}: {error}") from error

        if not isinstance(config, dict):
            raise ConfigError(f"{self.config_path.name} must contain a JSON object at the top level.")

        required_fields = ["assistant_name", "memory_path", "model", "llm_server"]
        for field_name in required_fields:
            if field_name not in config or not str(config[field_name]).strip():
                raise ConfigError(f"Missing required configuration value: {field_name}")

        memory_path = Path(config["memory_path"]).expanduser()
        if not memory_path.is_absolute():
            memory_path = (self.config_path.parent / memory_path).resolve()

        document_search = config.get("document_search", {})
        if not isinstance(document_search, dict):
            raise ConfigError("document_search must be an object")

        raw_directory_groups = document_search.get("directory_groups", {})
        if not isinstance(raw_directory_groups, dict):
            raise ConfigError("document_search.directory_groups must be an object")
        directory_groups: dict[str, set[str]] = {}
        for raw_name, raw_values in raw_directory_groups.items():
            group_name = normalize_config_token(str(raw_name))
            if not group_name:
                raise ConfigError("document_search.directory_groups cannot contain an empty group name")
            if group_name in directory_groups:
                raise ConfigError(f"Duplicate document_search.directory_groups entry after normalization: {raw_name}")
            if not isinstance(raw_values, list):
                raise ConfigError(f"document_search.directory_groups.{raw_name} must be a list")
            directory_groups[group_name] = {
                normalize_config_token(str(item))
                for item in raw_values
                if normalize_config_token(str(item))
            }

        raw_roots = document_search.get("roots", [])
        if not isinstance(raw_roots, list):
            raise ConfigError("document_search.roots must be a list")
        roots: list[DocumentSearchRoot] = []
        known_root_keys: set[str] = set()
        for index, value in enumerate(raw_roots):
            if isinstance(value, str):
                raw_path = value
                root_settings: dict[str, Any] = {}
            elif isinstance(value, dict):
                raw_path = str(value.get("path", ""))
                root_settings = value
            else:
                raise ConfigError(f"document_search.roots[{index}] must be a string path or object")

            if not raw_path.strip():
                raise ConfigError(f"document_search.roots[{index}] is missing a path")

            root_path = Path(raw_path).expanduser()
            if not root_path.is_absolute():
                root_path = (self.config_path.parent / root_path).resolve()
            root_key = normalize_root_key(root_path)
            if root_key in known_root_keys:
                raise ConfigError(f"Duplicate document_search root after normalization: {root_path}")
            known_root_keys.add(root_key)

            excluded_group_values = root_settings.get("excluded_directory_groups", [])
            if not isinstance(excluded_group_values, list):
                raise ConfigError(f"document_search.roots[{index}].excluded_directory_groups must be a list")
            excluded_groups = {
                normalize_config_token(str(item))
                for item in excluded_group_values
                if normalize_config_token(str(item))
            }
            unknown_groups = sorted(group_name for group_name in excluded_groups if group_name not in directory_groups)
            if unknown_groups:
                raise ConfigError(
                    "Unknown document_search directory group(s) for root "
                    f"{root_path}: {', '.join(unknown_groups)}"
                )

            excluded_directory_values = root_settings.get("excluded_directories", [])
            if not isinstance(excluded_directory_values, list):
                raise ConfigError(f"document_search.roots[{index}].excluded_directories must be a list")

            excluded_prefix_values = root_settings.get("excluded_directory_prefixes", [])
            if not isinstance(excluded_prefix_values, list):
                raise ConfigError(f"document_search.roots[{index}].excluded_directory_prefixes must be a list")

            excluded_relative_values = root_settings.get("excluded_relative_paths", [])
            if not isinstance(excluded_relative_values, list):
                raise ConfigError(f"document_search.roots[{index}].excluded_relative_paths must be a list")

            try:
                excluded_relative_paths = {
                    normalize_relative_path(str(item))
                    for item in excluded_relative_values
                    if str(item).strip()
                }
            except ValueError as error:
                raise ConfigError(f"document_search.roots[{index}].excluded_relative_paths contains an invalid path: {error}") from error

            roots.append(
                DocumentSearchRoot(
                    path=root_path,
                    excluded_directory_groups=excluded_groups,
                    excluded_directories={
                        normalize_config_token(str(item))
                        for item in excluded_directory_values
                        if normalize_config_token(str(item))
                    },
                    excluded_directory_prefixes={
                        normalize_config_token(str(item))
                        for item in excluded_prefix_values
                        if normalize_config_token(str(item))
                    },
                    excluded_relative_paths=excluded_relative_paths,
                )
            )

        excluded_directories = document_search.get("excluded_directories", [])
        if not isinstance(excluded_directories, list):
            raise ConfigError("document_search.excluded_directories must be a list")

        excluded_directory_prefixes = document_search.get("excluded_directory_prefixes", ["."])
        if not isinstance(excluded_directory_prefixes, list):
            raise ConfigError("document_search.excluded_directory_prefixes must be a list")

        supported_extensions = document_search.get("supported_extensions", [])
        if not isinstance(supported_extensions, list):
            raise ConfigError("document_search.supported_extensions must be a list")

        max_file_size_mb = int(document_search.get("max_file_size_mb", 100))
        if max_file_size_mb <= 0:
            raise ConfigError("document_search.max_file_size_mb must be positive")

        catalog_path_raw = document_search.get("catalog_path")
        if catalog_path_raw:
            catalog_path = Path(str(catalog_path_raw)).expanduser()
            if not catalog_path.is_absolute():
                catalog_path = (self.config_path.parent / catalog_path).resolve()
        else:
            memory_parent = memory_path.parent
            catalog_path = (memory_parent / "Index" / "documents.db").resolve()

        normalized_document_search = {
            "roots": roots,
            "directory_groups": directory_groups,
            "excluded_directories": [str(item).lower() for item in excluded_directories],
            "excluded_directory_prefixes": [str(item).lower() for item in excluded_directory_prefixes],
            "supported_extensions": [str(item).lower() for item in supported_extensions],
            "max_file_size_mb": max_file_size_mb,
            "catalog_path": catalog_path,
        }

        applications_cfg = config.get("applications", {})
        if not isinstance(applications_cfg, dict):
            raise ConfigError("applications must be an object")
        normalized_applications: dict[str, dict[str, Any]] = {}
        for app_id, payload in applications_cfg.items():
            if not isinstance(payload, dict):
                raise ConfigError(f"applications.{app_id} must be an object")
            executable_raw = str(payload.get("executable", "")).strip()
            if not executable_raw:
                raise ConfigError(f"applications.{app_id}.executable is required")
            executable_path = Path(executable_raw).expanduser()
            if not executable_path.is_absolute():
                executable_path = (self.config_path.parent / executable_path).resolve()
            aliases = payload.get("aliases", [])
            if not isinstance(aliases, list):
                raise ConfigError(f"applications.{app_id}.aliases must be a list")
            normalized_applications[str(app_id).lower()] = {
                "display_name": str(payload.get("display_name", app_id)),
                "executable": executable_path,
                "aliases": [str(alias).lower() for alias in aliases],
            }

        web_shortcuts_cfg = config.get("web_shortcuts", {})
        if not isinstance(web_shortcuts_cfg, dict):
            raise ConfigError("web_shortcuts must be an object")
        normalized_web_shortcuts = {str(key).lower(): str(value) for key, value in web_shortcuts_cfg.items() if str(value).strip()}

        action_audit_path_raw = config.get("action_audit_path")
        if action_audit_path_raw:
            action_audit_path = Path(str(action_audit_path_raw)).expanduser()
            if not action_audit_path.is_absolute():
                action_audit_path = (self.config_path.parent / action_audit_path).resolve()
        else:
            action_audit_path = (memory_path.parent / "Audit").resolve()

        memory_cfg = config.get("memory", {})
        if not isinstance(memory_cfg, dict):
            raise ConfigError("memory must be an object")
        memory_db_path_raw = memory_cfg.get("database_path")
        if memory_db_path_raw:
            memory_database_path = Path(str(memory_db_path_raw)).expanduser()
            if not memory_database_path.is_absolute():
                memory_database_path = (self.config_path.parent / memory_database_path).resolve()
        else:
            memory_database_path = (memory_path.parent / "Memory" / "conversations.db").resolve()

        normalized_memory = {
            "enabled": bool(memory_cfg.get("enabled", True)),
            "database_path": memory_database_path,
            "max_recent_messages": int(memory_cfg.get("max_recent_messages", 12)),
            "max_retrieved_topics": int(memory_cfg.get("max_retrieved_topics", 5)),
            "minimum_topic_score": float(memory_cfg.get("minimum_topic_score", 0.68)),
            "minimum_retrieval_score": float(memory_cfg.get("minimum_retrieval_score", 0.35)),
            "maximum_memory_tokens": int(memory_cfg.get("maximum_memory_tokens", 2500)),
            "maximum_supporting_excerpts": int(memory_cfg.get("maximum_supporting_excerpts", 6)),
            "maximum_supporting_excerpts_per_topic": int(memory_cfg.get("maximum_supporting_excerpts_per_topic", 3)),
            "maximum_excerpt_chars": int(memory_cfg.get("maximum_excerpt_chars", 280)),
            "include_current_topic": bool(memory_cfg.get("include_current_topic", True)),
            "diagnostics": bool(memory_cfg.get("diagnostics", False)),
            "semantic_weight": float(memory_cfg.get("semantic_weight", 0.75)),
            "current_topic_bonus": float(memory_cfg.get("current_topic_bonus", 0.12)),
            "recency_bonus_max": float(memory_cfg.get("recency_bonus_max", 0.13)),
        }

        llm_timeout_seconds = _normalize_timeout_seconds(config.get("llm_timeout_seconds", 30.0), field_name="llm_timeout_seconds")

        return {
            "assistant_name": str(config["assistant_name"]),
            "memory_path": memory_path,
            "session_path": Path(str(config.get("session_path", ""))).expanduser() if str(config.get("session_path", "")).strip() else None,
            "proposal_path": Path(str(config.get("proposal_path", ""))).expanduser() if str(config.get("proposal_path", "")).strip() else None,
            "audit_path": Path(str(config.get("audit_path", ""))).expanduser() if str(config.get("audit_path", "")).strip() else None,
            "model": str(config["model"]),
            "llm_server": str(config["llm_server"]),
            "llm_timeout_seconds": llm_timeout_seconds,
            "image_server": str(config.get("image_server", "")) if config.get("image_server") else None,
            "conversation": config.get("conversation", {}),
            "memory": normalized_memory,
            "memory_updates": config.get("memory_updates", {}),
            "general_knowledge": config.get("general_knowledge", {}),
            "document_search": normalized_document_search,
            "applications": normalized_applications,
            "web_shortcuts": normalized_web_shortcuts,
            "action_audit_path": action_audit_path,
        }


def _normalize_timeout_seconds(value: Any, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return 30.0
        if text in {"none", "infinite", "infinity", "null"}:
            return None
        try:
            numeric = float(text)
        except ValueError as error:
            raise ConfigError(f"{field_name} must be a positive number or null/none") from error
    elif isinstance(value, (int, float)):
        numeric = float(value)
    else:
        raise ConfigError(f"{field_name} must be a number or null")

    if numeric <= 0:
        return None
    return numeric
