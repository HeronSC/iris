# File: core/actions/implementations/add_document_root.py

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from core.actions.executor import ActionExecutionContext
from core.config.loader import ConfigLoader
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.config.document_search_mutations import build_confirmation_preview, ensure_document_search_section, ensure_root_entries, upsert_root_entry
from core.tools.models import PermissionLevel, ToolDefinition


class AddDocumentRootArguments(BaseModel):
    root: str = Field(description="Absolute folder path to add to the searchable document roots")


class AddDocumentRootAction:
    name = "add_document_root"
    definition = ToolDefinition(
        name="add_document_root",
        description="Add a folder to the searchable document roots in config.json.",
        arguments=AddDocumentRootArguments,
        permission=PermissionLevel.WRITE,
        requires_confirmation=True,
        expose_to_model=False,
    )

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        if context.config_path is None:
            return ValidationResult(ok=False, error="Configuration path is not available")

        root_raw = str(request.arguments.get("root", "")).strip()
        if not root_raw:
            return ValidationResult(ok=False, error="add_document_root requires root")

        root = Path(root_raw).expanduser()
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root

        if not resolved.exists() or not resolved.is_dir():
            return ValidationResult(ok=False, error="Document root does not exist or is not a directory", resolved_target=str(resolved))

        config_path = Path(context.config_path)
        try:
            with config_path.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
        except Exception:
            config = {}
        if not isinstance(config, dict):
            config = {}
        document_search = ensure_document_search_section(config)
        roots = ensure_root_entries(document_search)
        preview_roots = [item for item in roots]
        upsert_root_entry(preview_roots, str(resolved))

        return ValidationResult(
            ok=True,
            requires_confirmation=True,
            resolved_target=str(resolved),
            resolved_arguments={"root": str(resolved)},
            confirmation_preview=build_confirmation_preview(
                summary=f"Will add searchable root '{resolved}'.",
                target="document_search.roots",
                after_payload={"document_search": {"roots": preview_roots}},
                impact=f"Affects future index scans under {resolved}.",
                title="Searchable roots",
            ),
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        if context.config_path is None:
            return ActionResult(status="failed", message="Configuration path is not available", action=self.name, error="missing_config_path")

        root = Path(str(request.arguments.get("root", "")).strip()).expanduser()
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root

        config_path = Path(context.config_path)
        try:
            with config_path.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
        except Exception as error:
            return ActionResult(status="failed", message=f"Failed to read config: {error}", action=self.name, error="config_read_failed")

        if not isinstance(config, dict):
            return ActionResult(status="failed", message="Config file must contain a JSON object", action=self.name, error="invalid_config")

        document_search = ensure_document_search_section(config)
        roots = ensure_root_entries(document_search)
        upsert_root_entry(roots, str(resolved))

        try:
            with config_path.open("w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except Exception as error:
            return ActionResult(status="failed", message=f"Failed to write config: {error}", action=self.name, error="config_write_failed")

        loaded = ConfigLoader(config_path).load()
        loaded_document_search = loaded.get("document_search", {}) if isinstance(loaded, dict) else {}
        reloaded_roots = loaded_document_search.get("roots", []) if isinstance(loaded_document_search, dict) else []
        context.allowed_roots[:] = [item.path for item in reloaded_roots]
        if context.document_scanner is not None:
            context.document_scanner.config.roots[:] = reloaded_roots
            context.document_scanner.config.excluded_directories.clear()
            context.document_scanner.config.excluded_directories.update(set(loaded_document_search.get("excluded_directories", [])))
            context.document_scanner.config.excluded_directory_prefixes.clear()
            context.document_scanner.config.excluded_directory_prefixes.update(set(loaded_document_search.get("excluded_directory_prefixes", [])))
            context.document_scanner.config.directory_groups.clear()
            for key, value in loaded_document_search.get("directory_groups", {}).items():
                context.document_scanner.config.directory_groups[str(key)] = set(value)

        return ActionResult(status="success", message=f"Added document root '{resolved}'", action=self.name, resolved_target=str(resolved))