# File: core/assistant/action_commands.py

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from core.actions.audit import ActionAuditLogger
from core.actions.executor import ActionExecutor
from core.actions.models import ActionRequest
from core.assistant.output import OutputSink, emit_output
from core.assistant.prompting import PROMPT_CANCEL_TOKEN, PromptRequest, PromptType
from core.state.search_result_context import SearchResultContext


class ActionCommandHandler:
    def __init__(
        self,
        executor: ActionExecutor,
        audit_logger: ActionAuditLogger,
        search_context: SearchResultContext,
        output: OutputSink | None = None,
        prompt_provider: Callable[[PromptRequest | str], str] | None = None,
        application_catalog: Any | None = None,
        folder_finder: Callable[[str], list[Any]] | None = None,
    ) -> None:
        self.executor = executor
        self.audit_logger = audit_logger
        self.search_context = search_context
        self.output = output
        self.prompt_provider = prompt_provider
        self.application_catalog = application_catalog
        self.folder_finder = folder_finder

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        stripped = user_input.strip()

        if stripped == "/confirm":
            result = self.executor.confirm_pending()
            self._emit(result.message)
            return True

        if stripped == "/cancel":
            result = self.executor.cancel_pending()
            self._emit(result.message)
            return True

        if stripped.startswith("/open "):
            number = self._parse_number(stripped[len("/open "):])
            if number is None:
                self._emit("Usage: /open <result-number>")
                return True
            return self._execute_file_action("open_file", number, "Open selected search result")

        if stripped.startswith("/show "):
            number = self._parse_number(stripped[len("/show "):])
            if number is None:
                self._emit("Usage: /show <result-number>")
                return True
            return self._execute_file_action("show_in_explorer", number, "Show selected search result in Explorer")

        if stripped.startswith("/folder "):
            number = self._parse_number(stripped[len("/folder "):])
            if number is None:
                self._emit("Usage: /folder <result-number>")
                return True
            return self._execute_file_action("open_folder", number, "Open containing folder for selected search result")

        if stripped.startswith("/open-folder "):
            number = self._parse_number(stripped[len("/open-folder "):])
            if number is None:
                self._emit("Usage: /open-folder <result-number>")
                return True
            return self._execute_file_action("open_folder", number, "Open containing folder for selected search result")

        if stripped.startswith("/copy path "):
            number = self._parse_number(stripped[len("/copy path "):])
            if number is None:
                self._emit("Usage: /copy path <result-number>")
                return True
            item = self.search_context.get(number)
            if item is None:
                self._emit("No recent search result at that number.")
                return True
            result = self.executor.execute(
                ActionRequest(
                    action="copy_to_clipboard",
                    arguments={"text": item.path},
                    source="command",
                    reason="Copy selected path",
                )
            )
            self._emit(result.message)
            return True

        if stripped.startswith("/launch "):
            app_name = stripped[len("/launch "):].strip()
            if not app_name:
                self._emit("Usage: /launch <application-name>")
                return True
            return self._launch_with_optional_config_prompt(
                app_name=app_name,
                source="command",
                reason="Launch configured application",
            )

        if stripped.startswith("/open-url "):
            name = stripped[len("/open-url "):].strip()
            if not name:
                self._emit("Usage: /open-url <shortcut-name>")
                return True
            return self._open_url_with_optional_config_prompt(
                shortcut=name,
                source="command",
                reason="Open configured URL shortcut",
            )

        if stripped == "/actions recent":
            entries = self.audit_logger.read_recent(limit=10)
            if not entries:
                self._emit("No action history yet.")
                return True
            for entry in entries:
                self._emit(f"- {entry.get('created_at', '')} | {entry.get('action', '')} | {entry.get('status', '')} | {entry.get('message', '')}")
            return True

        return False

    def handle_natural_language(self, user_input: str) -> bool:
        lowered = user_input.strip().lower()

        if lowered in {"yes", "y", "confirm", "okay", "proceed", "do it", "go ahead"}:
            if self.executor.has_pending_confirmation():
                result = self.executor.confirm_pending()
                self._emit(result.message)
                return True
            if self.executor.consume_expired_confirmation_notice():
                self._emit("Pending action confirmation expired.")
                return True
            return False

        folder_match = re.search(r"(?:open|show|reveal).*(?:folder|containing folder).*?(?:number\s+)?(\d+)", lowered)
        if folder_match is not None:
            return self._execute_file_action("open_folder", int(folder_match.group(1)), "Natural-language open folder request")

        if re.search(r"(?:open|show|reveal).*(?:containing folder|open folder)", lowered) is not None:
            return self._execute_file_action("open_folder", 1, "Natural-language open folder request")

        open_match = re.search(r"open\s+(?:number\s+)?(\d+)", lowered)
        if open_match is not None:
            return self._execute_file_action("open_file", int(open_match.group(1)), "Natural-language open request")

        show_match = re.search(r"(?:show|reveal).*?(\d+)", lowered)
        if show_match is not None:
            return self._execute_file_action("show_in_explorer", int(show_match.group(1)), "Natural-language reveal request")

        launch_match = re.search(r"(?:launch|open)\s+([a-z0-9\-\s]+)$", lowered)
        if launch_match is not None and "file" not in lowered and "folder" not in lowered:
            candidate = launch_match.group(1).strip()
            shortcut_match = candidate.replace(" ", "-")
            if candidate in self.executor.context.web_shortcuts or shortcut_match in self.executor.context.web_shortcuts:
                shortcut = candidate if candidate in self.executor.context.web_shortcuts else shortcut_match
                result = self.executor.execute(
                    ActionRequest(
                        action="open_url",
                        arguments={"shortcut": shortcut},
                        source="natural-language",
                        reason="Natural-language URL request",
                    )
                )
                self._emit(result.message)
                return True

            result = self.executor.execute(
                ActionRequest(
                    action="launch_application",
                    arguments={"app_name": candidate},
                    source="natural-language",
                    reason="Natural-language launch request",
                )
            )
            if result.status == "failed" and result.error == "Unknown application":
                if lowered.startswith("open") and self._open_known_folder(candidate):
                    return True
                return self._launch_with_optional_config_prompt(
                    app_name=candidate,
                    source="natural-language",
                    reason="Natural-language launch request",
                )
            if result.status == "failed" and result.error == "unknown_action":
                return False
            if result.status == "failed" and result.error == "unknown_app":
                return False
            if result.status == "failed" and result.error == "unknown application":
                return False
            if result.status == "failed" and result.message.lower().startswith("unknown application"):
                return False
            self._emit(result.message)
            return True

        url_match = re.search(r"open\s+(?:url\s+)?([a-z0-9\-]+)$", lowered)
        if url_match is not None:
            shortcut = url_match.group(1)
            result = self.executor.execute(
                ActionRequest(
                    action="open_url",
                    arguments={"shortcut": shortcut},
                    source="natural-language",
                    reason="Natural-language URL request",
                )
            )
            if result.status == "failed" and result.error == "URL shortcut not found":
                return self._open_url_with_optional_config_prompt(
                    shortcut=shortcut,
                    source="natural-language",
                    reason="Natural-language URL request",
                )
            if result.status == "failed" and result.error == "unknown_shortcut":
                return False
            if result.status == "failed" and result.message.lower().startswith("url shortcut not found"):
                return False
            self._emit(result.message)
            return True

        return False

    def _execute_file_action(self, action: str, number: int, reason: str) -> bool:
        item = self.search_context.get(number)
        if item is None:
            self._emit("No recent search result at that number.")
            return True
        result = self.executor.execute(
            ActionRequest(
                action=action,
                arguments={"file_id": item.file_id},
                source="command" if reason.startswith("Open") or reason.startswith("Show") else "natural-language",
                reason=reason,
            )
        )
        self._emit(result.message)
        return True

    def _parse_number(self, value: str) -> int | None:
        stripped = value.strip()
        if not stripped.isdigit():
            return None
        return int(stripped)

    def _launch_with_optional_config_prompt(self, app_name: str, source: str, reason: str) -> bool:
        normalized_name = app_name.strip().lower()
        result = self.executor.execute(
            ActionRequest(
                action="launch_application",
                arguments={"app_name": normalized_name},
                source=source,
                reason=reason,
            )
        )
        if not self._is_unknown_application_result(result):
            self._emit(result.message)
            return True

        answer = self._ask(
            PromptRequest(
                prompt_id="add-missing-application",
                prompt_type=PromptType.YES_NO_CANCEL,
                text=f"Application '{app_name}' is not configured. Add it now?",
                choices=("yes", "no", "cancel"),
            )
        ).strip().lower()
        if answer in {PROMPT_CANCEL_TOKEN, "cancel"}:
            self._emit("Launch cancelled.")
            return True
        if answer not in {"y", "yes"}:
            self._emit("Launch cancelled.")
            return True

        suggested_name = ""
        executable = ""
        candidates = self._discover_applications(app_name)
        if candidates:
            browse = "Browse for the program..."
            choices = tuple(item.label for item in candidates) + (browse,)
            picked = self._ask(
                PromptRequest(
                    prompt_id="application-candidate",
                    prompt_type=PromptType.CHOICE,
                    text=f"Which program is '{app_name}'?",
                    choices=choices,
                )
            ).strip()
            if picked.lower() in {PROMPT_CANCEL_TOKEN, "cancel"}:
                self._emit("Launch cancelled.")
                return True
            match = next((item for item in candidates if picked in {item.label, item.name, item.executable}), None)
            if match is not None:
                executable = match.executable
                suggested_name = match.name
            elif picked != browse and picked.lower().endswith(".exe"):
                executable = picked
        if not executable:
            executable = self._ask(
                PromptRequest(
                    prompt_id="application-executable-path",
                    prompt_type=PromptType.FILE,
                    text=f"Executable path for '{app_name}':",
                )
            ).strip()
            if executable.lower() in {PROMPT_CANCEL_TOKEN, "cancel"}:
                self._emit("Launch cancelled.")
                return True
            if not executable:
                self._emit("Launch cancelled: executable path is required.")
                return True

        display_name = self._ask(
            PromptRequest(
                prompt_id="application-display-name",
                prompt_type=PromptType.TEXT,
                text=f"Display name [{suggested_name or app_name}]:",
            )
        ).strip()
        if display_name.lower() in {PROMPT_CANCEL_TOKEN, "cancel"}:
            self._emit("Launch cancelled.")
            return True
        display_name = display_name or suggested_name or app_name
        aliases_input = self._ask(
            PromptRequest(
                prompt_id="application-aliases",
                prompt_type=PromptType.TEXT,
                text=f"Aliases (comma-separated) [{app_name}]:",
            )
        ).strip()
        if aliases_input.lower() in {PROMPT_CANCEL_TOKEN, "cancel"}:
            self._emit("Launch cancelled.")
            return True
        aliases = [segment.strip().lower() for segment in aliases_input.split(",") if segment.strip()] if aliases_input else [normalized_name]
        app_id = re.sub(r"[^a-z0-9]+", "-", normalized_name).strip("-") or normalized_name.replace(" ", "-")

        update_result = self._apply_config_update(
            {
                "operation": "add_application",
                "app_id": app_id,
                "display_name": display_name,
                "executable": executable,
                "aliases": aliases,
            },
            reason=f"Add missing application '{app_name}' from {source} request",
        )
        if update_result is None:
            return True

        retry = self.executor.execute(
            ActionRequest(
                action="launch_application",
                arguments={"app_name": normalized_name},
                source=source,
                reason=reason,
            )
        )
        self._emit(retry.message)
        return True

    def _open_url_with_optional_config_prompt(self, shortcut: str, source: str, reason: str) -> bool:
        normalized = shortcut.strip().lower()
        result = self.executor.execute(
            ActionRequest(
                action="open_url",
                arguments={"shortcut": normalized},
                source=source,
                reason=reason,
            )
        )
        if not self._is_unknown_shortcut_result(result):
            self._emit(result.message)
            return True

        answer = self._ask(
            PromptRequest(
                prompt_id="add-missing-url-shortcut",
                prompt_type=PromptType.YES_NO_CANCEL,
                text=f"URL shortcut '{shortcut}' is not configured. Add it now?",
                choices=("yes", "no", "cancel"),
            )
        ).strip().lower()
        if answer in {PROMPT_CANCEL_TOKEN, "cancel"}:
            self._emit("Open URL cancelled.")
            return True
        if answer not in {"y", "yes"}:
            self._emit("Open URL cancelled.")
            return True

        url = self._ask(
            PromptRequest(
                prompt_id="url-shortcut-value",
                prompt_type=PromptType.TEXT,
                text=f"URL for shortcut '{shortcut}':",
            )
        ).strip()
        if url.lower() in {PROMPT_CANCEL_TOKEN, "cancel"}:
            self._emit("Open URL cancelled.")
            return True
        if not url:
            self._emit("Open URL cancelled: URL is required.")
            return True

        update_result = self._apply_config_update(
            {
                "operation": "set_web_shortcut",
                "name": normalized,
                "url": url,
            },
            reason=f"Add missing URL shortcut '{shortcut}' from {source} request",
        )
        if update_result is None:
            return True

        retry = self.executor.execute(
            ActionRequest(
                action="open_url",
                arguments={"shortcut": normalized},
                source=source,
                reason=reason,
            )
        )
        self._emit(retry.message)
        return True

    def _discover_applications(self, app_name: str) -> list[Any]:
        catalog = self.application_catalog
        if catalog is None:
            return []
        try:
            return list(catalog.find(app_name, limit=5))
        except Exception as error:
            self._emit(f"Could not scan installed programs: {error}")
            return []

    def _open_known_folder(self, name: str) -> bool:
        finder = self.folder_finder
        if finder is None:
            return False
        try:
            folders = list(finder(name))
        except Exception:
            return False
        if not folders:
            return False
        none_of_these = "None of these"
        choices = tuple(item.label for item in folders) + (none_of_these,)
        app_id = self._folder_application()
        app_label = self.executor.context.applications[app_id].display_name if app_id else "Explorer"
        picked = self._ask(
            PromptRequest(
                prompt_id="open-known-folder",
                prompt_type=PromptType.CHOICE,
                text=f"'{name}' looks like a folder. Open which one in {app_label}?",
                choices=choices,
            )
        ).strip()
        if picked.lower() in {PROMPT_CANCEL_TOKEN, "cancel"}:
            self._emit("Open cancelled.")
            return True
        match = next((item for item in folders if picked in {item.label, item.path}), None)
        if match is None:
            return False
        if app_id:
            result = self.executor.execute(
                ActionRequest(
                    action="launch_application",
                    arguments={"app_name": app_id, "target": match.path},
                    source="natural-language",
                    reason=f"Open folder '{name}' in {app_label}",
                )
            )
            self._emit(result.message)
            return True
        self.executor.context.system.open_folder(match.path)
        self._emit(f"Opened {match.path}")
        return True

    def _folder_application(self) -> str | None:
        applications = self.executor.context.applications
        for app_id, app in applications.items():
            haystack = " ".join([app_id, app.display_name, *app.aliases]).lower()
            if "code" in haystack:
                return app_id
        return None

    def _apply_config_update(self, arguments: dict[str, Any], reason: str):
        result = self.executor.execute(
            ActionRequest(
                action="update_config",
                arguments=arguments,
                source="interactive-prompt",
                reason=reason,
            )
        )
        if result.status == "pending_confirmation":
            result = self.executor.confirm_pending()

        self._emit(result.message)
        if result.status != "success":
            return None
        return result

    def _emit(self, text: str) -> None:
        emit_output(self.output, text)

    def _ask(self, request: PromptRequest) -> str:
        provider = self.prompt_provider or input
        if provider is None:
            return ""
        try:
            return provider(request)
        except TypeError:
            return provider(request.text)

    def _is_unknown_application_result(self, result: Any) -> bool:
        if result.status != "failed":
            return False
        if result.error in {"Unknown application", "unknown_app", "unknown application"}:
            return True
        return result.message.lower().startswith("unknown application")

    def _is_unknown_shortcut_result(self, result: Any) -> bool:
        if result.status != "failed":
            return False
        if result.error in {"URL shortcut not found", "unknown_shortcut"}:
            return True
        return result.message.lower().startswith("url shortcut not found")

