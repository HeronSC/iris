# File: core/assistant/intent_router.py

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.actions.executor import ActionExecutor
from core.actions.models import ActionRequest, ActionResult
from core.assistant.action_planner import ActionPlanner
from core.assistant.command_handler import CommandHandler
from core.assistant.conversation_synonyms import ConversationSynonymStore
from core.assistant.intent_example_store import IntentExampleStore
from core.assistant.llm_client import LLMClient
from core.assistant.output import OutputSink, emit_output
from core.llm.models import LLMRequest
from core.llm.ollama_client import OllamaClientError
from core.tools.models import ToolArgumentError, ToolDefinition, ToolKind
from core.tools.registry import ToolRegistry


class IndexScanArguments(BaseModel):
    root: str = Field(default="", description="Optional folder group to index, such as 'documents'; empty for all configured roots")


INTENT_SYSTEM_PROMPT = (
    "You are the intent layer of Iris, a local desktop assistant. "
    "Decide whether the user's message asks for an action that one of the available tools performs. "
    "If it does, call exactly one tool, taking its arguments from the message. "
    "If the message is ordinary conversation, a question, or asks for something no tool covers, "
    "reply with a short sentence and call no tool. "
    "Never invent paths, names, or values the user did not give."
)


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
        llm_client: LLMClient,
        index_handler: CommandHandler,
        memory_handler: CommandHandler,
        action_executor: ActionExecutor,
        example_store: IntentExampleStore,
        conversation_synonyms: ConversationSynonymStore | None = None,
        output: OutputSink | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.index_handler = index_handler
        self.memory_handler = memory_handler
        self.action_executor = action_executor
        self.example_store = example_store
        self.conversation_synonyms = conversation_synonyms
        self.output = output
        self.action_planner = ActionPlanner(action_executor.context)
        if tool_registry is None:
            action_registry = getattr(action_executor, "registry", None)
            tool_registry = getattr(action_registry, "tools", None) or ToolRegistry()
        self.tool_registry = tool_registry
        self._register_command_tools()
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

    def _register_command_tools(self) -> None:
        definitions = (
            ToolDefinition(
                name="index_scan",
                description="Re-index the user's configured document folders, or one named folder group such as 'documents'.",
                arguments=IndexScanArguments,
                kind=ToolKind.COMMAND,
                handler=self._run_index_scan,
            ),
            ToolDefinition(
                name="memory_scan",
                description="Scan the recent conversation for new facts worth remembering.",
                kind=ToolKind.COMMAND,
                handler=self._run_memory_scan,
            ),
            ToolDefinition(
                name="memory_review",
                description="Review the memory proposals waiting for the user's approval.",
                kind=ToolKind.COMMAND,
                handler=self._run_memory_review,
            ),
        )
        for definition in definitions:
            self.tool_registry.register(definition, replace=True)

    def _run_index_scan(self, arguments: dict[str, Any], state: dict[str, Any]) -> bool:
        root = str(arguments.get("root", "")).strip()
        command = "/index scan" if not root else f"/index scan {root}"
        return self.index_handler.handle(command, state)

    def _run_memory_scan(self, arguments: dict[str, Any], state: dict[str, Any]) -> bool:
        return self.memory_handler.handle("/memory scan", state)

    def _run_memory_review(self, arguments: dict[str, Any], state: dict[str, Any]) -> bool:
        return self.memory_handler.handle("/memory review", state)

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
        if any(token in lowered for token in keywords):
            return True
        return any(re.search(pattern, lowered) for pattern in self._registry_keyword_patterns())

    _KEYWORD_STOPWORDS = frozenset({"set", "add", "get", "the", "url", "to", "of", "for", "and"})

    def _registry_keywords(self) -> set[str]:
        words: set[str] = set()
        for name in self.tool_registry.names():
            definition = self.tool_registry.get(name)
            if definition is None or not definition.expose_to_model or not self.tool_registry.is_enabled(name):
                continue
            for token in re.split(r"[_\W]+", definition.name.lower()):
                if len(token) >= 3 and token not in self._KEYWORD_STOPWORDS:
                    words.add(token)
            for phrase in definition.keywords:
                cleaned = " ".join(phrase.lower().split())
                if cleaned:
                    words.add(cleaned)
        return words

    def _registry_keyword_patterns(self) -> list[str]:
        patterns: list[str] = []
        for word in self._registry_keywords():
            stem = word[:-1] if word.endswith("s") and len(word) > 3 else word
            patterns.append(rf"\b{re.escape(stem)}(?:s|es)?\b")
        return patterns

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

    PATH_ARGUMENTS: dict[str, tuple[str, ...]] = {
        "scan_document_root": ("root",),
        "config_remove_document_root": ("root",),
        "config_set_document_roots": ("roots",),
    }

    def _ungrounded_paths(self, text: str, intent_name: str, args: dict[str, Any]) -> list[str]:
        fields = self.PATH_ARGUMENTS.get(intent_name)
        if not fields:
            return []
        haystack = self._normalize_path_text(text)
        missing: list[str] = []
        for field in fields:
            value = args.get(field)
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                needle = self._normalize_path_text(str(candidate or ""))
                if needle and needle not in haystack:
                    missing.append(str(candidate))
        return missing

    @staticmethod
    def _normalize_path_text(value: str) -> str:
        collapsed = re.sub(r"\s+", " ", value.strip().lower())
        return collapsed.replace("/", "\\").rstrip("\\")

    def _dispatch_intent(self, text: str, resolved: StructuredIntent, state: dict[str, Any]) -> bool:
        intent_name = resolved.intent
        args = resolved.arguments

        if resolved.source == "llm":
            invented = self._ungrounded_paths(text, intent_name, args)
            if invented:
                if intent_name == "scan_document_root":
                    fallback = StructuredIntent(intent="index_scan", arguments={"root": ""}, source=resolved.source)
                    return self._dispatch_intent(text, fallback, state)
                self._last_resolution = (text, resolved)
                self._emit("Which folder? I did not see a folder path in your message.")
                return True

        if intent_name == "scan_document_root":
            root = str(args.get("root", "")).strip()
            if not root:
                return False
            self._last_resolution = (text, resolved)
            result = self._dispatch_scan_document_root(root)
            if self._should_learn_from_result(result):
                self._save_successful_resolution(text, resolved)
            return True

        definition = self.tool_registry.get(intent_name)
        if definition is None or not self.tool_registry.is_enabled(intent_name):
            return False

        if definition.kind == ToolKind.COMMAND:
            handler = definition.handler
            if handler is None:
                return False
            try:
                arguments = definition.validate_arguments(args)
            except ToolArgumentError as error:
                self._emit(f"I could not run {intent_name}: {error}")
                return True
            self._last_resolution = (text, resolved)
            handled = bool(handler(arguments, state))
            if handled:
                self._save_successful_resolution(text, resolved)
            return handled

        if definition.kind == ToolKind.ACTION:
            self._last_resolution = (text, resolved)
            result = self._execute_action(
                action=intent_name,
                arguments=dict(args),
                source=resolved.source,
                reason=f"Intent router: {intent_name}",
            )
            if self._should_learn_from_result(result):
                self._save_successful_resolution(text, resolved)
            return True

        return False

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
        tools = self.tool_registry.model_tools()
        if not tools:
            return None

        system_prompt = INTENT_SYSTEM_PROMPT
        learned_examples = self.example_store.render_examples_for_prompt()
        if learned_examples:
            system_prompt = (
                system_prompt
                + "\nPast requests and the tool call each one mapped to (intent = tool name):\n"
                + learned_examples
            )

        request = LLMRequest.from_prompts(system_prompt, text, tools=tools, think=False, task="intent")
        try:
            response = self.llm_client.chat(request)
        except OllamaClientError:
            return None

        for call in response.tool_calls:
            definition = self.tool_registry.get(call.name)
            if definition is None or not definition.expose_to_model or not self.tool_registry.is_enabled(call.name):
                continue
            return {"intent": call.name, "arguments": dict(call.arguments)}
        return None
