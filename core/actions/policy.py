from __future__ import annotations

from core.actions.models import ActionRequest


class ActionPolicy:
    def requires_confirmation(self, request: ActionRequest) -> bool:
        if request.action in {"open_file", "open_folder", "show_in_explorer", "launch_application", "copy_to_clipboard", "scan_document_root"}:
            return False
        if request.action == "open_url":
            return False
        return True

