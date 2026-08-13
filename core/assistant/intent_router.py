from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.actions.executor import ActionExecutor
from core.actions.models import ActionRequest, ActionResult
from core.assistant.action_planner import ActionPlanner
from core.assistant.command_handler import CommandHandler
from core.assistant.conversation_synonyms import ConversationSynonymStore
from core.assistant.intent_example_store import IntentExampleStore
from core.assistant.output import OutputSink, emit_output
from core.llm.ollama_client import OllamaClient, OllamaClientError


@dataclass(frozen=True)
class StructuredIntent:
    intent: str
    arguments: dict[str, Any]
    source: str


@dataclass(frozen=True)
class PendingClarification:
    kind: str
    source_text: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class PendingPhraseCapture:
    category: str


class IntentRouter:
    def __init__(
        self,
        llm_client: OllamaClient,
        index_handler: CommandHandler,
        memory_handler: CommandHandler,
        action_executor: ActionExecutor,
        example_store: IntentExampleStore,
        conversation_synonyms: ConversationSynonymStore | None = None,
        output: OutputSink | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.index_handler = index_handler
        self.memory_handler = memory_handler
        self.action_executor = action_executor
        self.example_store = example_store
        self.conversation_synonyms = conversation_synonyms
        self.output = output
        self.action_planner = ActionPlanner(action_executor.context)
        self._last_resolution: tuple[str, StructuredIntent] | None = None
        self._pending_correction_source: str | None = None
        self._pending_correction_resolution: StructuredIntent | None = None
        self._pending_clarification: PendingClarification | None = None
        self._pending_phrase_capture: PendingPhraseCapture | None = None

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        text = user_input.strip()
        if not text or text.startswith("/"):
            return False

        if self._handle_pending_phrase_capture(text):
            return True

        direct_phrase_store = self._extract_direct_phrase_store_request(text)
        if direct_phrase_store is not None:
            category, phrases = direct_phrase_store
            return self._store_phrase_list(category, phrases)

        phrase_category = self._extract_phrase_teaching_request(text)
        if phrase_category is not None:
            self._pending_phrase_capture = PendingPhraseCapture(category=phrase_category)
            self._emit(f"Share the {phrase_category} phrases you want me to remember. A comma-separated list is fine.")
            return True

        if self._handle_pending_clarification(text, state):
            return True

        ambiguous_stop_indexing_root = self._extract_ambiguous_stop_indexing_request(text)
        if ambiguous_stop_indexing_root is not None:
            self._pending_clarification = PendingClarification(
                kind="stop_indexing",
                source_text=text,
                arguments={"root": ambiguous_stop_indexing_root},
            )
            self._emit("Did you mean:\n1. Stop the currently running scan?\n2. Remove this folder from future indexing?")
            return True

        if self._handle_correction_flow(text, state):
            return True

        resolved = self._resolve_intent(text)
        if resolved is None:
            return False

        return self._dispatch_intent(text, resolved, state)

    def _execute_action(self, action: str, arguments: dict[str, Any], source: str, reason: str) -> ActionResult:
        result = self.action_executor.execute(
            ActionRequest(
                action=action,
                arguments=arguments,
                source=source,
                reason=reason,
            )
        )
        self._emit(result.message)
        return result

    def _execute_action_request(self, request: ActionRequest) -> ActionResult:
        result = self.action_executor.execute(request)
        self._emit(result.message)
        return result

    def _looks_actionable(self, text: str) -> bool:
        lowered = text.lower()
        keywords = [
            "index",
            "scan",
            "profile",
            "memory",
            "config",
            "settings",
            "setting",
            "model",
            "assistant name",
            "application",
            "app",
            "shortcut",
            "document root",
            "search root",
            "change",
            "update",
            "set",
            "stop indexing",
            "remove",
            "delete",
            "open",
            "launch",
        ]
        return any(token in lowered for token in keywords)

    def _resolve_intent(self, text: str) -> StructuredIntent | None:
        normalized_text = self._normalize_action_text(text)

        if self.conversation_synonyms is not None:
            alias = self.conversation_synonyms.resolve_alias(text)
            if alias is not None:
                return StructuredIntent(intent=alias.intent, arguments=alias.arguments, source="conversation-alias")

        stored = self.example_store.resolve(text)
        if stored is not None:
            return StructuredIntent(intent=stored.intent, arguments=stored.arguments, source="example-store")

        direct_index_root = self._extract_index_root_request(normalized_text)
        if direct_index_root is not None:
            return StructuredIntent(intent="scan_document_root", arguments={"root": direct_index_root}, source="heuristic")

        if not self._looks_actionable(text) and not self._looks_actionable(normalized_text):
            return None

        parsed = self._classify(normalized_text)
        if parsed is None:
            return None

        intent_name = str(parsed.get("intent", "none")).strip().lower()
        arguments = parsed.get("arguments", {})
        if intent_name == "none" or not isinstance(arguments, dict):
            return None
        return StructuredIntent(intent=intent_name, arguments=arguments, source="llm")

    def _normalize_action_text(self, text: str) -> str:
        normalized = text.strip()
        if not normalized:
            return normalized

        normalized = re.sub(r"^(?:hi|hello|hey)\b[\s,!.-]*", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\b(?:could you|can you|would you|will you|please)\b", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+", " ", normalized).strip()

        replacements = (
            (r"\bgo through what you remember\b", "review memory"),
            (r"\bwhat do you remember\b", "review memory"),
            (r"\bwhat did you remember\b", "review memory"),
            (r"\bre[-\s]?index\b", "index"),
        )
        for pattern, replacement in replacements:
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

        return re.sub(r"\s+", " ", normalized).strip()

    def _extract_index_root_request(self, text: str) -> str | None:
        lowered = text.lower()
        if not any(token in lowered for token in {"index", "scan"}):
            return None
        if self._looks_like_index_removal_request(lowered):
            return None

        quoted_match = re.search(r'([A-Za-z]:\\[^"\n\r]+)', text)
        if quoted_match is not None:
            return quoted_match.group(1).strip()

        return None

    def _extract_ambiguous_stop_indexing_request(self, text: str) -> str | None:
        lowered = text.lower()
        if "stop indexing" not in lowered:
            return None
        if any(marker in lowered for marker in {"this folder", "future indexing", "no longer index", "remove from index", "remove this folder"}):
            return None

        match = re.search(r'([A-Za-z]:\\[^"\n\r]+)', text)
        if match is None:
            return None
        return match.group(1).strip()

    def _extract_phrase_teaching_request(self, text: str) -> str | None:
        lowered = text.lower()
        if any(token in lowered for token in {"confirm", "confirmation"}) and any(token in lowered for token in {"word", "words", "phrase", "phrases"}):
            return "confirm"
        if any(token in lowered for token in {"cancel", "cancellation"}) and any(token in lowered for token in {"word", "words", "phrase", "phrases"}):
            return "cancel"
        return None

    def _extract_direct_phrase_store_request(self, text: str) -> tuple[str, list[str]] | None:
        lowered = text.lower()
        if not any(token in lowered for token in {"store", "stored", "save", "remember", "use"}):
            return None

        phrases = self._extract_phrase_list(text)
        if len(phrases) < 2:
            return None

        if any(token in lowered for token in {"confirm", "confirmation"}):
            return "confirm", phrases
        if any(token in lowered for token in {"cancel", "cancellation"}):
            return "cancel", phrases
        return None

    def _looks_like_index_removal_request(self, lowered: str) -> bool:
        removal_markers = [
            "stop indexing",
            "no longer index",
            "don't index",
            "do not index",
            "remove from index",
            "remove this folder from index",
            "stop scanning",
        ]
        return any(marker in lowered for marker in removal_markers)

    def _dispatch_intent(self, text: str, resolved: StructuredIntent, state: dict[str, Any]) -> bool:
        intent_name = resolved.intent
        args = resolved.arguments

        if intent_name == "scan_document_root":
            root = str(args.get("root", "")).strip()
            if not root:
                return False
            self._last_resolution = (text, resolved)
            result = self._dispatch_scan_document_root(root)
            if self._should_learn_from_result(result):
                self._save_successful_resolution(text, resolved)
            return True

        if intent_name == "index_scan":
            root = str(args.get("root", "")).strip()
            command = "/index scan" if not root else f"/index scan {root}"
            self._last_resolution = (text, resolved)
            handled = self.index_handler.handle(command, state)
            if handled:
                self._save_successful_resolution(text, resolved)
            return handled

        if intent_name == "memory_scan":
            self._last_resolution = (text, resolved)
            handled = self.memory_handler.handle("/memory scan", state)
            if handled:
                self._save_successful_resolution(text, resolved)
            return handled

        if intent_name == "memory_review":
            self._last_resolution = (text, resolved)
            handled = self.memory_handler.handle("/memory review", state)
            if handled:
                self._save_successful_resolution(text, resolved)
            return handled

        if intent_name == "launch_application":
            self._last_resolution = (text, resolved)
            result = self._execute_action(
                action="launch_application",
                arguments={"app_name": args.get("app_name", "")},
                source=resolved.source,
                reason="Intent router: launch application",
            )
            if self._should_learn_from_result(result):
                self._save_successful_resolution(text, resolved)
            return True

        if intent_name == "open_url_shortcut":
            self._last_resolution = (text, resolved)
            result = self._execute_action(
                action="open_url",
                arguments={"shortcut": args.get("shortcut", "")},
                source=resolved.source,
                reason="Intent router: open URL shortcut",
            )
            if self._should_learn_from_result(result):
                self._save_successful_resolution(text, resolved)
            return True

        mapping = {
            "config_add_application": (
                "update_config",
                {
                    "operation": "add_application",
                    "app_id": args.get("app_id", ""),
                    "display_name": args.get("display_name", ""),
                    "executable": args.get("executable", ""),
                    "aliases": args.get("aliases", []),
                },
                "Intent router: add application to config",
            ),
            "config_set_document_roots": (
                "update_config",
                {"operation": "set_document_roots", "roots": args.get("roots", [])},
                "Intent router: update document search roots",
            ),
            "config_remove_document_root": (
                "update_config",
                {"operation": "remove_document_root", "root": args.get("root", "")},
                "Intent router: stop indexing a document root",
            ),
            "config_set_web_shortcut": (
                "update_config",
                {"operation": "set_web_shortcut", "name": args.get("name", ""), "url": args.get("url", "")},
                "Intent router: update web shortcut",
            ),
            "config_remove_web_shortcut": (
                "update_config",
                {"operation": "remove_web_shortcut", "name": args.get("name", "")},
                "Intent router: remove web shortcut",
            ),
            "config_remove_application": (
                "update_config",
                {"operation": "remove_application", "app_id": args.get("app_id", "")},
                "Intent router: remove application",
            ),
            "config_set_value": (
                "update_config",
                {"operation": "set_value", "key": args.get("key", ""), "value": args.get("value")},
                "Intent router: set config value",
            ),
            "profile_update": (
                "update_profile",
                {"updates": args.get("updates", {})},
                "Intent router: update user profile",
            ),
        }
        details = mapping.get(intent_name)
        if details is None:
            return False

        self._last_resolution = (text, resolved)
        action_name, arguments, reason = details
        result = self._execute_action(action=action_name, arguments=arguments, source=resolved.source, reason=reason)
        if self._should_learn_from_result(result):
            self._save_successful_resolution(text, resolved)
        return True

    def _dispatch_scan_document_root(self, root: str) -> ActionResult:
        plan = self.action_planner.plan_make_directory_searchable(root)
        return self._execute_action_request(plan.request)

    def _save_successful_resolution(self, text: str, resolved: StructuredIntent) -> None:
        self.example_store.save_example(text, resolved.intent, resolved.arguments, source=f"successful:{resolved.source}")

    def _should_learn_from_result(self, result: ActionResult) -> bool:
        return result.status in {"success", "pending_confirmation"}

    def _handle_correction_flow(self, text: str, state: dict[str, Any]) -> bool:
        lowered = text.lower()
        if self._pending_correction_resolution is not None:
            if lowered in {"y", "yes", "confirm", "okay", "do it", "go ahead"}:
                source_text = self._pending_correction_source or ""
                resolution = self._pending_correction_resolution
                self.example_store.save_example(
                    source_text,
                    resolution.intent,
                    resolution.arguments,
                    source="user-correction",
                    corrected_from=source_text,
                )
                self._pending_correction_resolution = None
                self._pending_correction_source = None
                return self._dispatch_intent(source_text, resolution, state)
            if lowered in {"n", "no", "cancel"}:
                self._pending_correction_resolution = None
                self._pending_correction_source = None
                self._emit("Correction discarded.")
                return True
            self._emit("Confirm the correction naturally, or cancel it.")
            return True

        if self._pending_correction_source is not None:
            corrected = self._resolve_intent(text)
            if corrected is None:
                self._emit("I still could not determine the intended action. Please describe it more explicitly.")
                return True
            self._pending_correction_resolution = corrected
            self._emit("I think I understand. Save this correction for future routing and perform it now? [y/N]:")
            return True

        if lowered in {"that is not what i meant", "that's not what i meant", "not what i meant"}:
            if self._last_resolution is None:
                self._emit("I do not have a recent routed action to correct.")
                return True
            self._pending_correction_source = self._last_resolution[0]
            self._emit("What action did you want instead?")
            return True

        return False

    def _handle_pending_clarification(self, text: str, state: dict[str, Any]) -> bool:
        clarification = self._pending_clarification
        if clarification is None:
            return False

        lowered = text.strip().lower()
        if lowered in {"cancel", "never mind", "nevermind", "forget it", "no"}:
            self._pending_clarification = None
            self._emit("Clarification cancelled.")
            return True

        if clarification.kind != "stop_indexing":
            self._pending_clarification = None
            return False

        root = str(clarification.arguments.get("root", "")).strip()
        if lowered in {"2", "remove it", "remove it from future indexing", "remove this folder", "future indexing", "remove from future indexing"}:
            self._pending_clarification = None
            resolved = StructuredIntent(
                intent="config_remove_document_root",
                arguments={"root": root},
                source="clarification",
            )
            return self._dispatch_intent(clarification.source_text, resolved, state)

        if lowered in {"1", "stop the current scan", "stop the currently running scan", "stop the scan", "current scan", "running scan"}:
            self._pending_clarification = None
            self._emit("Stopping an active scan is not supported yet. If you want this folder removed from future indexing, say that explicitly.")
            return True

        self._emit("Please reply with 1 to stop the current scan, 2 to remove the folder from future indexing, or cancel.")
        return True

    def _handle_pending_phrase_capture(self, text: str) -> bool:
        pending = self._pending_phrase_capture
        if pending is None:
            return False

        lowered = text.strip().lower()
        if lowered in {"cancel", "never mind", "nevermind", "forget it", "no"}:
            self._pending_phrase_capture = None
            self._emit("Phrase update cancelled.")
            return True

        if self.conversation_synonyms is None:
            self._pending_phrase_capture = None
            self._emit("Conversation synonyms are not available.")
            return True

        phrases = self._extract_phrase_list(text)
        if not phrases:
            self._emit("I did not find any phrases to store. Send them as a comma-separated list, or say cancel.")
            return True

        self._pending_phrase_capture = None
        return self._store_phrase_list(pending.category, phrases)

    def _extract_phrase_list(self, text: str) -> list[str]:
        candidate = text.strip()
        if ":" in candidate:
            candidate = candidate.split(":", 1)[1].strip()
        elif "." in candidate and "," in candidate:
            candidate = candidate.rsplit(".", 1)[1].strip()

        parts = [segment.strip(" '\"\t") for segment in re.split(r",|\n", candidate)]
        return [item for item in parts if item]

    def _store_phrase_list(self, category: str, phrases: list[str]) -> bool:
        if self.conversation_synonyms is None:
            self._emit("Conversation synonyms are not available.")
            return True

        added = self.conversation_synonyms.add_pending_response_phrases(category, phrases)
        if added:
            joined = ", ".join(added)
            self._emit(f"Stored {category} phrases: {joined}")
            return True

        self._emit(f"Those {category} phrases were already stored.")
        return True

    def _emit(self, text: str) -> None:
        emit_output(self.output, text)

    def _classify(self, text: str) -> dict[str, Any] | None:
        learned_examples = self.example_store.render_examples_for_prompt()
        system_prompt = (
            "You are an intent classifier for a local assistant. "
            "Return JSON only with keys: intent and arguments. "
            "Supported intents: none, index_scan, memory_scan, memory_review, "
            "scan_document_root, launch_application, open_url_shortcut, "
            "config_add_application, config_set_document_roots, config_remove_document_root, "
            "config_set_web_shortcut, config_remove_web_shortcut, config_remove_application, "
            "config_set_value, profile_update. "
            "Use intent none when request is ordinary chat.\n"
            "Examples:\n"
            "- 'index my documents folder' -> {\"intent\":\"index_scan\",\"arguments\":{\"root\":\"documents\"}}\n"
            "- 'index D:\\\\HenryZuraw\\\\Documents' -> {\"intent\":\"scan_document_root\",\"arguments\":{\"root\":\"D:\\\\HenryZuraw\\\\Documents\"}}\n"
            "- 'scan memory updates' -> {\"intent\":\"memory_scan\",\"arguments\":{}}\n"
            "- 'review memory proposals' -> {\"intent\":\"memory_review\",\"arguments\":{}}\n"
            "- 'launch visual studio code' -> {\"intent\":\"launch_application\",\"arguments\":{\"app_name\":\"visual studio code\"}}\n"
            "- 'open bc-sandbox' -> {\"intent\":\"open_url_shortcut\",\"arguments\":{\"shortcut\":\"bc-sandbox\"}}\n"
            "- 'add application excel at C:\\\\Program Files\\\\...\\\\EXCEL.EXE aliases excel' -> "
            "{\"intent\":\"config_add_application\",\"arguments\":{\"app_id\":\"excel\",\"display_name\":\"Excel\",\"executable\":\"C:\\\\Program Files\\\\...\\\\EXCEL.EXE\",\"aliases\":[\"excel\"]}}\n"
            "- 'set document roots to E:\\\\Docs and C:\\\\Users\\\\me\\\\Documents' -> "
            "{\"intent\":\"config_set_document_roots\",\"arguments\":{\"roots\":[\"E:\\\\Docs\",\"C:\\\\Users\\\\me\\\\Documents\"]}}\n"
            "- 'would you please stop indexing this folder D:\\\\Docs' -> "
            "{\"intent\":\"config_remove_document_root\",\"arguments\":{\"root\":\"D:\\\\Docs\"}}\n"
            "- 'add web shortcut github to https://github.com' -> "
            "{\"intent\":\"config_set_web_shortcut\",\"arguments\":{\"name\":\"github\",\"url\":\"https://github.com\"}}\n"
            "- 'remove web shortcut github' -> "
            "{\"intent\":\"config_remove_web_shortcut\",\"arguments\":{\"name\":\"github\"}}\n"
            "- 'delete application vscode' -> "
            "{\"intent\":\"config_remove_application\",\"arguments\":{\"app_id\":\"vscode\"}}\n"
            "- 'set model to qwen3:8b' -> "
            "{\"intent\":\"config_set_value\",\"arguments\":{\"key\":\"model\",\"value\":\"qwen3:8b\"}}\n"
            "- 'update my profile display_name to Henry' -> "
            "{\"intent\":\"profile_update\",\"arguments\":{\"updates\":{\"display_name\":\"Henry\"}}}"
        )
        if learned_examples:
            system_prompt = system_prompt + "\nLearned examples:\n" + learned_examples

        try:
            raw = self.llm_client.generate(system_prompt, text)
        except OllamaClientError:
            return None

        parsed = self._parse_json(raw)
        if parsed is None:
            return None

        intent_name = str(parsed.get("intent", "none")).strip().lower()
        allowed = {
            "none",
            "index_scan",
            "scan_document_root",
            "memory_scan",
            "memory_review",
            "launch_application",
            "open_url_shortcut",
            "config_add_application",
            "config_set_document_roots",
            "config_remove_document_root",
            "config_set_web_shortcut",
            "config_remove_web_shortcut",
            "config_remove_application",
            "config_set_value",
            "profile_update",
        }
        if intent_name not in allowed:
            return None

        return parsed

    def _parse_json(self, content: str) -> dict[str, Any] | None:
        body = content.strip()
        if body.startswith("```"):
            body = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", body)
            body = re.sub(r"\s*```$", "", body)
            body = body.strip()
        start = body.find("{")
        end = body.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return None
        snippet = body[start : end + 1]
        try:
            parsed = json.loads(snippet)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed
