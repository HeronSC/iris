# File: core/actions/executor.py

from __future__ import annotations

import os
import subprocess
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.actions.audit import ActionAuditLogger
from core.actions.models import ActionRequest, ActionResult, ApplicationConfig, ConfirmationPreview, ValidationResult
from core.documents.scanner import DocumentScanner
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.documents.catalog import DocumentCatalog
from core.observability.request_context import current_request_id


class SystemAdapter:
    def _start_file(self, path: str) -> None:
        start_file = getattr(os, "startfile", None)
        if start_file is None:
            raise OSError("Opening paths is not supported on this platform")
        start_file(path)

    def open_file(self, path: str) -> None:
        self._start_file(path)

    def open_folder(self, path: str) -> None:
        self._start_file(path)

    def show_in_explorer(self, path: str) -> None:
        subprocess.run(["explorer.exe", "/select,", path], check=False)

    def launch_application(self, executable: str) -> None:
        subprocess.Popen([executable])

    def open_url(self, url: str) -> None:
        webbrowser.open(url, new=2)

    def copy_text(self, text: str) -> None:
        process = subprocess.Popen(["clip"], stdin=subprocess.PIPE, text=True)
        process.communicate(text)


@dataclass(frozen=True)
class ActionExecutionContext:
    catalog: DocumentCatalog
    allowed_roots: list[Path]
    applications: dict[str, ApplicationConfig]
    app_alias_map: dict[str, str]
    web_shortcuts: dict[str, str]
    system: SystemAdapter
    config_path: Path | None = None
    memory_path: Path | None = None
    document_scanner: DocumentScanner | None = None


@dataclass(frozen=True)
class PendingAction:
    original_request: ActionRequest
    validated_request: ActionRequest
    resolved_target: str | None
    expires_at: datetime
    follow_up_request: ActionRequest | None
    confirmation_preview: ConfirmationPreview | None


class ActionExecutor:
    def __init__(
        self,
        registry: ActionRegistry,
        policy: ActionPolicy,
        audit: ActionAuditLogger,
        context: ActionExecutionContext,
        confirmation_ttl_seconds: int = 120,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.audit = audit
        self.context = context
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self._pending_action: PendingAction | None = None
        self._expired_confirmation_notice = False

    def execute(self, request: ActionRequest) -> ActionResult:
        tool_name = request.action
        resolved = self.registry.resolve(request)
        if resolved is None:
            result = ActionResult(status="failed", message="Unknown action type", action=request.action, error="unknown_action")
            self._log(request, result)
            return result
        if not self.registry.is_enabled(tool_name):
            result = ActionResult(status="failed", message=f"Tool is disabled: {tool_name}", action=request.action, error="tool_disabled")
            self._log(request, result)
            return result
        if resolved.error:
            result = ActionResult(status="failed", message=f"Invalid arguments: {resolved.error}", action=request.action, error="invalid_arguments")
            self._log(request, result)
            return result

        action = resolved.action
        request = resolved.request
        definitions = [resolved.definition]
        if resolved.definition.is_facet:
            underlying = self.registry.definition(resolved.definition.target_action)
            if underlying is not None:
                definitions.append(underlying)

        validation = action.validate(request, self.context)
        if not validation.ok:
            result = ActionResult(
                status="failed",
                message=validation.error or "Action validation failed",
                action=request.action,
                resolved_target=validation.resolved_target,
                error=validation.error,
                confirmation_preview=validation.confirmation_preview,
            )
            self._log(request, result)
            return result

        validated_arguments = request.arguments
        if validation.resolved_arguments is not None:
            validated_arguments = validation.resolved_arguments
        validated_request = ActionRequest(
            action=request.action,
            arguments=validated_arguments,
            source=request.source,
            reason=request.reason,
            follow_up=request.follow_up,
            workflow_goal=request.workflow_goal,
            workflow_parameters=request.workflow_parameters,
        )

        needs_confirmation = validation.requires_confirmation or any(
            self.policy.requires_confirmation(request, definition) for definition in definitions
        )
        if needs_confirmation:
            if self.has_pending_confirmation():
                result = ActionResult(
                    status="rejected",
                    message="Another action is already awaiting confirmation.",
                    action=request.action,
                    resolved_target=validation.resolved_target,
                    error="confirmation_already_pending",
                )
                self._log(request, result, tool=tool_name)
                return result
            self._pending_action = PendingAction(
                original_request=request,
                validated_request=validated_request,
                resolved_target=validation.resolved_target,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.confirmation_ttl_seconds),
                follow_up_request=request.follow_up,
                confirmation_preview=validation.confirmation_preview,
            )
            self._expired_confirmation_notice = False
            description = self._describe_request(validated_request, validation.resolved_target)
            result = ActionResult(
                status="pending_confirmation",
                message=(
                    "Awaiting confirmation.\n\n"
                    f"{description}\n\n"
                    "Reply naturally to confirm or cancel, or tell me what to change."
                ),
                action=request.action,
                resolved_target=validation.resolved_target,
                error="confirmation_required",
                confirmation_preview=validation.confirmation_preview,
            )
            self._log(request, result, tool=tool_name)
            return result

        result = self._execute_validated(validated_request, validation.resolved_target)
        self._log(request, result, tool=tool_name)
        return result

    def confirm_pending(self) -> ActionResult:
        if self._pending_action is None:
            return ActionResult(
                status="rejected",
                message="No pending action to confirm.",
                action="confirm",
                error="no_pending_action",
            )

        pending = self._pending_action
        self._pending_action = None

        if datetime.now(timezone.utc) > pending.expires_at:
            request = ActionRequest(action="confirm", arguments={}, source="command", reason="Expired confirmation")
            result = ActionResult(
                status="rejected",
                message="Pending action confirmation expired.",
                action=pending.validated_request.action,
                resolved_target=pending.resolved_target,
                error="confirmation_expired",
                confirmation_preview=pending.confirmation_preview,
            )
            self._log(request, result)
            return result

        result = self._execute_validated(pending.validated_request, pending.resolved_target)
        if result.status == "success" and pending.follow_up_request is not None:
            follow_up_result = self.execute(pending.follow_up_request)
            result = ActionResult(
                status=follow_up_result.status,
                message=f"{result.message}\n{follow_up_result.message}",
                action=follow_up_result.action,
                resolved_target=follow_up_result.resolved_target or result.resolved_target,
                error=follow_up_result.error,
                confirmation_preview=follow_up_result.confirmation_preview,
            )
        confirm_request = ActionRequest(
            action=pending.validated_request.action,
            arguments=pending.validated_request.arguments,
            source="confirm",
            reason=f"Confirmed: {pending.original_request.reason}",
            workflow_goal=pending.validated_request.workflow_goal,
            workflow_parameters=pending.validated_request.workflow_parameters,
        )
        self._log(confirm_request, result)
        return result

    def has_pending_confirmation(self) -> bool:
        if self._pending_action is None:
            return False
        if datetime.now(timezone.utc) > self._pending_action.expires_at:
            self._pending_action = None
            self._expired_confirmation_notice = True
            return False
        return True

    def consume_expired_confirmation_notice(self) -> bool:
        if not self._expired_confirmation_notice:
            return False
        self._expired_confirmation_notice = False
        return True

    def cancel_pending(self) -> ActionResult:
        if not self.has_pending_confirmation():
            return ActionResult(
                status="rejected",
                message="No pending action to cancel.",
                action="cancel",
                error="no_pending_action",
            )

        pending = self._pending_action
        self._pending_action = None
        self._expired_confirmation_notice = False
        result = ActionResult(
            status="cancelled",
            message="Pending action was cancelled.",
            action=pending.validated_request.action if pending is not None else "cancel",
            resolved_target=pending.resolved_target if pending is not None else None,
            error=None,
            confirmation_preview=pending.confirmation_preview if pending is not None else None,
        )
        cancel_request = ActionRequest(action="cancel", arguments={}, source="command", reason="Cancelled pending action")
        self._log(cancel_request, result)
        return result

    def pending_description(self) -> str | None:
        if not self.has_pending_confirmation() or self._pending_action is None:
            return None
        return self._describe_request(self._pending_action.validated_request, self._pending_action.resolved_target)

    def pending_confirmation_preview(self) -> ConfirmationPreview | None:
        if not self.has_pending_confirmation() or self._pending_action is None:
            return None
        return self._pending_action.confirmation_preview

    def _execute_validated(self, request: ActionRequest, resolved_target: str | None) -> ActionResult:
        action = self.registry.get(request.action)
        if action is None:
            return ActionResult(
                status="failed",
                message="Unknown action type",
                action=request.action,
                resolved_target=resolved_target,
                error="unknown_action",
            )

        try:
            result = action.execute(request, self.context)
        except Exception as error:
            result = ActionResult(
                status="failed",
                message=f"Action failed: {error}",
                action=request.action,
                resolved_target=resolved_target,
                error=str(error),
            )
        return result

    def _log(self, request: ActionRequest, result: ActionResult, tool: str | None = None) -> None:
        self.audit.log(
            {
                "request_id": current_request_id(),
                "action": request.action,
                "tool": tool if tool and tool != request.action else request.action,
                "arguments": request.arguments,
                "source": request.source,
                "reason": request.reason,
                "workflow_goal": request.workflow_goal,
                "workflow_parameters": request.workflow_parameters,
                "resolved_target": result.resolved_target,
                "status": result.status,
                "message": result.message,
                "error": result.error,
                "created_at": result.created_at,
            }
        )

    def _describe_request(self, request: ActionRequest, resolved_target: str | None) -> str:
        if self._pending_action is not None and self._pending_action.validated_request == request:
            preview = self._pending_action.confirmation_preview
            if preview is not None and preview.summary.strip():
                return preview.summary.strip()
        if request.action == "add_document_root":
            root = str(request.arguments.get("root", resolved_target or "")).strip()
            return f"Add {root} to searchable folders (document_search.roots in config.json)"
        if request.action == "scan_document_root":
            root = str(request.arguments.get("root", resolved_target or "")).strip()
            return f"Scan {root}"
        if request.action == "launch_application":
            app_name = str(request.arguments.get("app_name", resolved_target or "the application")).strip()
            return f"Launch {app_name}"
        if request.action == "open_url":
            shortcut = str(request.arguments.get("shortcut", resolved_target or "the shortcut")).strip()
            return f"Open URL shortcut {shortcut}"
        if request.action == "update_profile":
            return "Update your profile"
        if request.action == "update_config":
            operation = str(request.arguments.get("operation", "")).strip()
            if operation == "remove_document_root":
                root = str(request.arguments.get("root", resolved_target or "")).strip()
                return f"Remove {root} from indexing"
            if operation == "set_document_roots":
                return "Update configured document roots"
            if operation == "add_application":
                app_id = str(request.arguments.get("app_id", "the application")).strip()
                return f"Add application {app_id}"
            if operation == "remove_application":
                app_id = str(request.arguments.get("app_id", "the application")).strip()
                return f"Remove application {app_id}"
            if operation == "set_web_shortcut":
                name = str(request.arguments.get("name", "the shortcut")).strip()
                return f"Save web shortcut {name}"
            if operation == "remove_web_shortcut":
                name = str(request.arguments.get("name", "the shortcut")).strip()
                return f"Remove web shortcut {name}"
            if operation == "set_value":
                key = str(request.arguments.get("key", "setting")).strip()
                return f"Update setting {key}"
        return request.reason or request.action


def path_is_allowed(path: Path, allowed_roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False

