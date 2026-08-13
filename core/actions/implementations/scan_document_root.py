from __future__ import annotations

from pathlib import Path

from core.actions.executor import ActionExecutionContext
from core.actions.models import ActionRequest, ActionResult, ValidationResult


class ScanDocumentRootAction:
    name = "scan_document_root"

    def validate(self, request: ActionRequest, context: ActionExecutionContext) -> ValidationResult:
        if context.document_scanner is None:
            return ValidationResult(ok=False, error="Document scanner is not available")

        root_raw = str(request.arguments.get("root", "")).strip()
        if not root_raw:
            return ValidationResult(ok=False, error="scan_document_root requires root")

        root = Path(root_raw).expanduser()
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root

        if not resolved.exists() or not resolved.is_dir():
            return ValidationResult(ok=False, error="Document root does not exist or is not a directory", resolved_target=str(resolved))

        return ValidationResult(
            ok=True,
            resolved_target=str(resolved),
            resolved_arguments={"root": str(resolved)},
        )

    def execute(self, request: ActionRequest, context: ActionExecutionContext) -> ActionResult:
        if context.document_scanner is None:
            return ActionResult(status="failed", message="Document scanner is not available", action=self.name, error="missing_document_scanner")

        root = str(request.arguments.get("root", "")).strip()
        prior_errors = context.catalog.list_scan_errors(limit=20)
        previous_marker = None
        if prior_errors:
            first = prior_errors[0]
            previous_marker = (first.get("path", ""), first.get("error", ""), first.get("created_at", ""))
        try:
            result = context.document_scanner.scan(root_filter=root)
        except ValueError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, resolved_target=root, error="scan_root_invalid")

        latest_errors = context.catalog.list_scan_errors(limit=20)
        if previous_marker is None:
            new_error_entries = latest_errors
        else:
            new_error_entries = []
            for entry in latest_errors:
                marker = (entry.get("path", ""), entry.get("error", ""), entry.get("created_at", ""))
                if marker == previous_marker:
                    break
                new_error_entries.append(entry)

        message = (
            "Scan complete: "
            f"scanned={result.scanned_files}, indexed={result.indexed_files}, "
            f"updated={result.updated_files}, deleted={result.deleted_files}, errors={result.error_files}"
        )

        if new_error_entries:
            preview = []
            for entry in new_error_entries[:3]:
                preview.append(f"- {entry.get('path', '')}: {entry.get('error', '')}")
            message = message + "\nRecent scan errors:\n" + "\n".join(preview)
        return ActionResult(status="success", message=message, action=self.name, resolved_target=root)