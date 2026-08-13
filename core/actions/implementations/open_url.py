from __future__ import annotations

from urllib.parse import urlparse

from core.actions.executor import ActionExecutionContext
from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.config.document_search_mutations import build_confirmation_preview


class OpenUrlAction:
    name = "open_url"

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        shortcut = str(request.arguments.get("shortcut", "")).strip().lower()
        url = str(request.arguments.get("url", "")).strip()

        configured = False
        if shortcut:
            if shortcut not in context.web_shortcuts:
                return ValidationResult(ok=False, error="URL shortcut not found")
            url = context.web_shortcuts[shortcut]
            configured = True

        if not url:
            return ValidationResult(ok=False, error="Missing URL")

        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"}:
            return ValidationResult(ok=False, error="Unsafe URL scheme", resolved_target=url)
        if not parsed.hostname:
            return ValidationResult(ok=False, error="URL must include a valid host", resolved_target=url)
        if parsed.username or parsed.password:
            return ValidationResult(ok=False, error="URLs containing credentials are not allowed", resolved_target=url)

        if parsed.scheme == "http" and not configured:
            return ValidationResult(ok=False, error="Unconfigured http URLs are not allowed", resolved_target=url)

        return ValidationResult(
            ok=True,
            requires_confirmation=not configured,
            resolved_target=url,
            resolved_arguments={"url": url},
            confirmation_preview=(
                None
                if configured
                else build_confirmation_preview(
                    summary=f"Will open URL '{url}'.",
                    target=url,
                    after_payload={"url": url},
                    impact="Opens the target in your default browser.",
                    title="Open URL",
                )
            ),
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        url = str(request.arguments.get("url", "")).strip()
        if not url:
            return ActionResult(status="failed", message="Missing URL", action=self.name, error="missing_url")

        context.system.open_url(url)
        return ActionResult(status="success", message="URL opened", action=self.name, resolved_target=url)

