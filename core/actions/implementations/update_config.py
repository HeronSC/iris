# File: core/actions/implementations/update_config.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.actions.executor import ActionExecutionContext
from core.actions.models import ApplicationConfig, ActionRequest, ActionResult, ValidationResult
from core.config.document_search_mutations import (
    build_confirmation_preview,
    ensure_document_search_section,
    ensure_root_entries,
    remove_root_entry,
)
from core.config.loader import ConfigLoader
from core.documents.models import root_entry_to_json
from core.tools.models import PermissionLevel, ToolDefinition


class UpdateConfigArguments(BaseModel):
    operation: str = Field(description="One of add_application, set_document_roots, remove_document_root, set_web_shortcut, remove_web_shortcut, remove_application, set_value")
    app_id: str | None = None
    display_name: str | None = None
    executable: str | None = None
    aliases: list[str] | None = None
    roots: list[str] | None = None
    root: str | None = None
    name: str | None = None
    url: str | None = None
    key: str | None = None
    value: Any = None


class AddApplicationArguments(BaseModel):
    app_id: str = Field(description="Short identifier for the application, for example 'excel'")
    display_name: str = Field(description="Human-readable application name")
    executable: str = Field(description="Full path to the executable")
    aliases: list[str] = Field(default_factory=list, description="Other names the user may call it")


class SetDocumentRootsArguments(BaseModel):
    roots: list[str] = Field(description="Absolute folder paths that replace the current searchable roots")


class RemoveDocumentRootArguments(BaseModel):
    root: str = Field(description="Absolute folder path to stop indexing")


class SetWebShortcutArguments(BaseModel):
    name: str = Field(description="Shortcut name, for example 'github'")
    url: str = Field(description="Full URL the shortcut opens")


class RemoveWebShortcutArguments(BaseModel):
    name: str = Field(description="Shortcut name to remove")


class RemoveApplicationArguments(BaseModel):
    app_id: str = Field(description="Identifier of the application to remove")


class SetConfigValueArguments(BaseModel):
    key: Literal["assistant_name", "model", "llm_server", "image_server"] = Field(description="Setting to change")
    value: str | int | float | bool = Field(description="New value for the setting")


class UpdateConfigAction:
    name = "update_config"
    definition = ToolDefinition(
        name="update_config",
        description="Change a setting in config.json.",
        arguments=UpdateConfigArguments,
        permission=PermissionLevel.WRITE,
        requires_confirmation=True,
        expose_to_model=False,
    )
    facets = (
        ToolDefinition(
            name="config_add_application",
            description="Register a new application so Iris can launch it by name.",
            arguments=AddApplicationArguments,
            permission=PermissionLevel.WRITE,
            requires_confirmation=True,
            bind={"operation": "add_application"},
        ),
        ToolDefinition(
            name="config_set_document_roots",
            description="Replace the list of folders Iris indexes for document search.",
            arguments=SetDocumentRootsArguments,
            permission=PermissionLevel.WRITE,
            requires_confirmation=True,
            bind={"operation": "set_document_roots"},
        ),
        ToolDefinition(
            name="config_remove_document_root",
            description="Stop indexing a folder: remove it from the searchable document roots.",
            arguments=RemoveDocumentRootArguments,
            permission=PermissionLevel.WRITE,
            requires_confirmation=True,
            bind={"operation": "remove_document_root"},
        ),
        ToolDefinition(
            name="config_set_web_shortcut",
            description="Save or update a named web shortcut.",
            arguments=SetWebShortcutArguments,
            permission=PermissionLevel.WRITE,
            requires_confirmation=True,
            bind={"operation": "set_web_shortcut"},
        ),
        ToolDefinition(
            name="config_remove_web_shortcut",
            description="Remove a saved web shortcut.",
            arguments=RemoveWebShortcutArguments,
            permission=PermissionLevel.WRITE,
            requires_confirmation=True,
            bind={"operation": "remove_web_shortcut"},
        ),
        ToolDefinition(
            name="config_remove_application",
            description="Remove a registered application from Iris.",
            arguments=RemoveApplicationArguments,
            permission=PermissionLevel.WRITE,
            requires_confirmation=True,
            bind={"operation": "remove_application"},
        ),
        ToolDefinition(
            name="config_set_value",
            description="Set a top-level Iris setting: assistant_name, model, llm_server, or image_server.",
            arguments=SetConfigValueArguments,
            permission=PermissionLevel.WRITE,
            requires_confirmation=True,
            bind={"operation": "set_value"},
        ),
    )
    ALLOWED_SET_VALUE_KEYS = {"assistant_name", "model", "llm_server", "image_server"}

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        if context.config_path is None:
            return ValidationResult(ok=False, error="Configuration path is not available")

        config_path = Path(context.config_path)
        config = self._load_config(config_path)
        operation = str(request.arguments.get("operation", "")).strip().lower()
        if operation == "add_application":
            app_id = str(request.arguments.get("app_id", "")).strip().lower()
            display_name = str(request.arguments.get("display_name", "")).strip()
            executable = str(request.arguments.get("executable", "")).strip()
            aliases_raw = request.arguments.get("aliases", [])
            if not app_id or not display_name or not executable:
                return ValidationResult(ok=False, error="add_application requires app_id, display_name, and executable")
            if not isinstance(aliases_raw, list):
                return ValidationResult(ok=False, error="aliases must be a list")
            aliases = [str(item).strip().lower() for item in aliases_raw if str(item).strip()]
            if not aliases:
                aliases = [app_id]
            return ValidationResult(
                ok=True,
                requires_confirmation=True,
                resolved_target=str(context.config_path),
                resolved_arguments={
                    "operation": operation,
                    "app_id": app_id,
                    "display_name": display_name,
                    "executable": executable,
                    "aliases": aliases,
                },
                confirmation_preview=self._preview_add_application(config, app_id, display_name, executable, aliases),
            )

        if operation == "set_document_roots":
            roots_raw = request.arguments.get("roots", [])
            if not isinstance(roots_raw, list):
                return ValidationResult(ok=False, error="set_document_roots requires roots as a list")
            roots = [str(item).strip() for item in roots_raw if str(item).strip()]
            if not roots:
                return ValidationResult(ok=False, error="set_document_roots requires at least one root")
            return ValidationResult(
                ok=True,
                requires_confirmation=True,
                resolved_target=str(context.config_path),
                resolved_arguments={"operation": operation, "roots": roots},
                confirmation_preview=self._preview_set_document_roots(config, roots),
            )

        if operation == "remove_document_root":
            root = str(request.arguments.get("root", "")).strip()
            if not root:
                return ValidationResult(ok=False, error="remove_document_root requires root")
            return ValidationResult(
                ok=True,
                requires_confirmation=True,
                resolved_target=str(context.config_path),
                resolved_arguments={"operation": operation, "root": root},
                confirmation_preview=self._preview_remove_document_root(config, root),
            )

        if operation == "set_web_shortcut":
            name = str(request.arguments.get("name", "")).strip().lower()
            url = str(request.arguments.get("url", "")).strip()
            if not name or not url:
                return ValidationResult(ok=False, error="set_web_shortcut requires name and url")
            if not (url.startswith("https://") or url.startswith("http://")):
                return ValidationResult(ok=False, error="Shortcut URL must start with http:// or https://")
            return ValidationResult(
                ok=True,
                requires_confirmation=True,
                resolved_target=str(context.config_path),
                resolved_arguments={"operation": operation, "name": name, "url": url},
                confirmation_preview=self._preview_set_web_shortcut(config, name, url),
            )

        if operation == "remove_web_shortcut":
            name = str(request.arguments.get("name", "")).strip().lower()
            if not name:
                return ValidationResult(ok=False, error="remove_web_shortcut requires name")
            return ValidationResult(
                ok=True,
                requires_confirmation=True,
                resolved_target=str(context.config_path),
                resolved_arguments={"operation": operation, "name": name},
                confirmation_preview=self._preview_remove_web_shortcut(config, name),
            )

        if operation == "remove_application":
            app_id = str(request.arguments.get("app_id", "")).strip().lower()
            if not app_id:
                return ValidationResult(ok=False, error="remove_application requires app_id")
            return ValidationResult(
                ok=True,
                requires_confirmation=True,
                resolved_target=str(context.config_path),
                resolved_arguments={"operation": operation, "app_id": app_id},
                confirmation_preview=self._preview_remove_application(config, app_id),
            )

        if operation == "set_value":
            key = str(request.arguments.get("key", "")).strip()
            if key not in self.ALLOWED_SET_VALUE_KEYS:
                allowed = ", ".join(sorted(self.ALLOWED_SET_VALUE_KEYS))
                return ValidationResult(ok=False, error=f"set_value key must be one of: {allowed}")
            if "value" not in request.arguments:
                return ValidationResult(ok=False, error="set_value requires value")
            value = request.arguments.get("value")
            if not isinstance(value, (str, int, float, bool, list, dict, type(None))):
                return ValidationResult(ok=False, error="set_value value must be JSON-compatible")
            return ValidationResult(
                ok=True,
                requires_confirmation=True,
                resolved_target=str(context.config_path),
                resolved_arguments={"operation": operation, "key": key, "value": value},
                confirmation_preview=self._preview_set_value(config, key, value),
            )

        return ValidationResult(ok=False, error="Unsupported config operation")

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        if context.config_path is None:
            return ActionResult(status="failed", message="Configuration path is not available", action=self.name, error="missing_config_path")

        config_path = Path(context.config_path)
        config = self._load_config(config_path)

        operation = str(request.arguments.get("operation", "")).strip().lower()

        if operation == "add_application":
            app_id = str(request.arguments.get("app_id", "")).strip().lower()
            display_name = str(request.arguments.get("display_name", "")).strip()
            executable = str(request.arguments.get("executable", "")).strip()
            aliases = request.arguments.get("aliases", [])
            applications = config.get("applications", {})
            if not isinstance(applications, dict):
                applications = {}
            applications[app_id] = {
                "display_name": display_name,
                "executable": executable,
                "aliases": aliases,
            }
            config["applications"] = applications
            context.applications[app_id] = ApplicationConfig(
                id=app_id,
                display_name=display_name,
                executable=executable,
                aliases=[str(item).strip().lower() for item in aliases if str(item).strip()],
            )
            context.app_alias_map[app_id] = app_id
            context.app_alias_map[display_name.lower()] = app_id
            for alias in context.applications[app_id].aliases:
                context.app_alias_map[alias] = app_id
            message = f"Added/updated application '{app_id}'"

        elif operation == "set_document_roots":
            roots = request.arguments.get("roots", [])
            document_search = ensure_document_search_section(config)
            document_search["roots"] = [root_entry_to_json(root) for root in roots if str(root).strip()]
            message = "Updated document search roots"

        elif operation == "remove_document_root":
            root = str(request.arguments.get("root", "")).strip()
            document_search = ensure_document_search_section(config)
            roots = ensure_root_entries(document_search)
            updated_roots = remove_root_entry(roots, root)
            removed = len(updated_roots) != len(roots)
            document_search["roots"] = updated_roots
            if removed:
                message = f"Removed document root '{root}'"
            else:
                message = f"Document root was not configured: '{root}'"

        elif operation == "set_web_shortcut":
            shortcuts = config.get("web_shortcuts", {})
            if not isinstance(shortcuts, dict):
                shortcuts = {}
            name = str(request.arguments.get("name", "")).strip().lower()
            url = str(request.arguments.get("url", "")).strip()
            shortcuts[name] = url
            config["web_shortcuts"] = shortcuts
            context.web_shortcuts[name] = url
            message = f"Updated web shortcut '{name}'"

        elif operation == "remove_web_shortcut":
            shortcuts = config.get("web_shortcuts", {})
            if not isinstance(shortcuts, dict):
                shortcuts = {}
            name = str(request.arguments.get("name", "")).strip().lower()
            removed = name in shortcuts
            shortcuts.pop(name, None)
            config["web_shortcuts"] = shortcuts
            context.web_shortcuts.pop(name, None)
            if removed:
                message = f"Removed web shortcut '{name}'"
            else:
                message = f"Web shortcut was not configured: '{name}'"

        elif operation == "remove_application":
            applications = config.get("applications", {})
            if not isinstance(applications, dict):
                applications = {}
            app_id = str(request.arguments.get("app_id", "")).strip().lower()
            removed = app_id in applications
            applications.pop(app_id, None)
            config["applications"] = applications

            removed_aliases = []
            if app_id in context.applications:
                removed_aliases = context.applications[app_id].aliases
                context.applications.pop(app_id, None)
            for alias, mapped_id in list(context.app_alias_map.items()):
                if mapped_id == app_id:
                    context.app_alias_map.pop(alias, None)
            for alias in removed_aliases:
                context.app_alias_map.pop(alias, None)

            if removed:
                message = f"Removed application '{app_id}'"
            else:
                message = f"Application was not configured: '{app_id}'"

        elif operation == "set_value":
            key = str(request.arguments.get("key", "")).strip()
            if key not in self.ALLOWED_SET_VALUE_KEYS:
                return ActionResult(status="failed", message="Unsupported set_value key", action=self.name, error="unsupported_set_value_key")
            config[key] = request.arguments.get("value")
            message = f"Updated config key '{key}'"

        else:
            return ActionResult(status="failed", message="Unsupported config operation", action=self.name, error="unsupported_operation")

        try:
            with config_path.open("w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2)
                handle.write("\n")
        except Exception as error:
            return ActionResult(status="failed", message=f"Failed to write config: {error}", action=self.name, error="config_write_failed")

        self._reload_runtime_config(context, config_path)

        return ActionResult(status="success", message=message, action=self.name, resolved_target=str(config_path))

    def _load_config(self, config_path: Path) -> dict[str, Any]:
        try:
            with config_path.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    def _reload_runtime_config(self, context: ActionExecutionContext, config_path: Path) -> None:
        loaded = ConfigLoader(config_path).load()
        document_search = loaded.get("document_search", {}) if isinstance(loaded, dict) else {}
        if isinstance(document_search, dict):
            roots = document_search.get("roots", [])
            if isinstance(roots, list):
                context.allowed_roots[:] = [item.path for item in roots]
                if context.document_scanner is not None:
                    context.document_scanner.config.roots[:] = roots
                    context.document_scanner.config.excluded_directories.clear()
                    context.document_scanner.config.excluded_directories.update(set(document_search.get("excluded_directories", [])))
                    context.document_scanner.config.excluded_directory_prefixes.clear()
                    context.document_scanner.config.excluded_directory_prefixes.update(set(document_search.get("excluded_directory_prefixes", [])))
                    context.document_scanner.config.directory_groups.clear()
                    for key, value in document_search.get("directory_groups", {}).items():
                        context.document_scanner.config.directory_groups[str(key)] = set(value)

    def _preview_add_application(self, config: dict[str, Any], app_id: str, display_name: str, executable: str, aliases: list[str]):
        applications = config.get("applications", {})
        if not isinstance(applications, dict):
            applications = {}
        applications = dict(applications)
        applications[app_id] = {
            "display_name": display_name,
            "executable": executable,
            "aliases": aliases,
        }
        return build_confirmation_preview(
            summary=f"Will update application '{app_id}'.",
            target="applications",
            after_payload={"applications": applications},
            impact=f"Affects launch requests that resolve to '{app_id}'.",
            title="Applications",
        )

    def _preview_set_document_roots(self, config: dict[str, Any], roots: list[str]):
        document_search = ensure_document_search_section(json.loads(json.dumps(config)))
        document_search["roots"] = [root_entry_to_json(root) for root in roots]
        return build_confirmation_preview(
            summary="Will replace configured searchable roots.",
            target="document_search.roots",
            after_payload={"document_search": {"roots": document_search["roots"]}},
            impact="Affects future index scans and allowed file actions.",
            title="Searchable roots",
        )

    def _preview_remove_document_root(self, config: dict[str, Any], root: str):
        document_search = ensure_document_search_section(json.loads(json.dumps(config)))
        roots = ensure_root_entries(document_search)
        document_search["roots"] = remove_root_entry(roots, root)
        return build_confirmation_preview(
            summary=f"Will remove searchable root '{root}'.",
            target="document_search.roots",
            after_payload={"document_search": {"roots": document_search["roots"]}},
            impact=f"Affects future index scans and file actions under {root}.",
            title="Searchable roots",
        )

    def _preview_set_web_shortcut(self, config: dict[str, Any], name: str, url: str):
        shortcuts = config.get("web_shortcuts", {})
        if not isinstance(shortcuts, dict):
            shortcuts = {}
        shortcuts = dict(shortcuts)
        shortcuts[name] = url
        return build_confirmation_preview(
            summary=f"Will update web shortcut '{name}'.",
            target="web_shortcuts",
            after_payload={"web_shortcuts": shortcuts},
            impact=f"Affects future open-url requests for '{name}'.",
            title="Web shortcuts",
        )

    def _preview_remove_web_shortcut(self, config: dict[str, Any], name: str):
        shortcuts = config.get("web_shortcuts", {})
        if not isinstance(shortcuts, dict):
            shortcuts = {}
        shortcuts = dict(shortcuts)
        shortcuts.pop(name, None)
        return build_confirmation_preview(
            summary=f"Will remove web shortcut '{name}'.",
            target="web_shortcuts",
            after_payload={"web_shortcuts": shortcuts},
            impact=f"Affects future open-url requests for '{name}'.",
            title="Web shortcuts",
        )

    def _preview_remove_application(self, config: dict[str, Any], app_id: str):
        applications = config.get("applications", {})
        if not isinstance(applications, dict):
            applications = {}
        applications = dict(applications)
        applications.pop(app_id, None)
        return build_confirmation_preview(
            summary=f"Will remove application '{app_id}'.",
            target="applications",
            after_payload={"applications": applications},
            impact=f"Affects future launch requests for '{app_id}'.",
            title="Applications",
        )

    def _preview_set_value(self, config: dict[str, Any], key: str, value: Any):
        preview = dict(config)
        preview[key] = value
        return build_confirmation_preview(
            summary=f"Will update config key '{key}'.",
            target=key,
            after_payload={key: preview[key]},
            title="Configuration",
        )
