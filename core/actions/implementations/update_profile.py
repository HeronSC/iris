# File: core/actions/implementations/update_profile.py

from __future__ import annotations

from typing import Any

import json
from pathlib import Path

from pydantic import BaseModel, Field

from core.actions.executor import ActionExecutionContext
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.config.document_search_mutations import build_confirmation_preview
from core.actions.diffs import with_file_diff
from core.tools.models import PermissionLevel, ToolDefinition


class UpdateProfileArguments(BaseModel):
    updates: dict[str, Any] = Field(description="Profile fields to set, for example {'display_name': 'Henry'}")


class UpdateProfileAction:
    name = "update_profile"
    definition = ToolDefinition(
        name="update_profile",
        description="Update fields in the user's profile.",
        arguments=UpdateProfileArguments,
        permission=PermissionLevel.WRITE,
        requires_confirmation=True,
        expose_to_model=False,
    )
    facets = (
        ToolDefinition(
            name="profile_update",
            description="Update the user's profile, such as their display name or preferences.",
            arguments=UpdateProfileArguments,
            permission=PermissionLevel.WRITE,
            requires_confirmation=True,
        ),
    )

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        if context.memory_path is None:
            return ValidationResult(ok=False, error="Memory path is not available")

        updates_raw = request.arguments.get("updates", {})
        if not isinstance(updates_raw, dict):
            return ValidationResult(ok=False, error="updates must be an object")

        updates = {str(key).strip(): value for key, value in updates_raw.items() if str(key).strip()}
        if not updates:
            return ValidationResult(ok=False, error="update_profile requires at least one profile field")

        if any(not isinstance(value, (str, int, float, bool, list, dict, type(None))) for value in updates.values()):
            return ValidationResult(ok=False, error="profile field values must be JSON-compatible")

        profile_path = Path(context.memory_path) / "profile.json"
        try:
            with profile_path.open("r", encoding="utf-8-sig") as handle:
                profile_payload = json.load(handle)
        except (OSError, ValueError, RuntimeError, TypeError):
            profile_payload = {}
        if not isinstance(profile_payload, dict):
            profile_payload = {}
        profile_section = profile_payload.get("profile", {})
        if not isinstance(profile_section, dict):
            profile_section = {}
        preview_profile = dict(profile_section)
        preview_profile.update(updates)
        after_payload = dict(profile_payload)
        after_payload["profile"] = preview_profile
        preview = build_confirmation_preview(
            summary=f"Will update profile fields: {', '.join(sorted(updates.keys()))}.",
            target="profile",
            after_payload={"profile": preview_profile},
            impact="Affects future profile-aware responses.",
            title="Profile",
        )
        return ValidationResult(
            ok=True,
            requires_confirmation=True,
            resolved_target=str(profile_path),
            resolved_arguments={"updates": updates},
            confirmation_preview=with_file_diff(preview, profile_path, after_payload),
            changes=(str(profile_path),),
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        if context.memory_path is None:
            return ActionResult(status="failed", message="Memory path is not available", action=self.name, error="missing_memory_path")

        profile_path = Path(context.memory_path) / "profile.json"
        try:
            with profile_path.open("r", encoding="utf-8-sig") as handle:
                profile_payload = json.load(handle)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ActionResult(status="failed", message=f"Failed to read profile memory: {error}", action=self.name, error="profile_read_failed")

        if not isinstance(profile_payload, dict):
            return ActionResult(status="failed", message="profile.json must contain a JSON object", action=self.name, error="invalid_profile_json")

        profile_section = profile_payload.get("profile", {})
        if not isinstance(profile_section, dict):
            profile_section = {}

        updates = request.arguments.get("updates", {})
        if not isinstance(updates, dict):
            return ActionResult(status="failed", message="Missing validated updates object", action=self.name, error="missing_updates")

        profile_section.update(updates)
        profile_payload["profile"] = profile_section

        try:
            with profile_path.open("w", encoding="utf-8") as handle:
                json.dump(profile_payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ActionResult(status="failed", message=f"Failed to write profile memory: {error}", action=self.name, error="profile_write_failed")

        updated_keys = ", ".join(sorted(updates.keys()))
        return ActionResult(
            status="success",
            message=f"Updated profile fields: {updated_keys}",
            action=self.name,
            resolved_target=str(profile_path),
        )