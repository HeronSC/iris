from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.assistant.general_knowledge_router import GeneralKnowledgeRouter
from core.assistant.llm_client import LLMClient
from core.assistant.orchestrator import AssistantOrchestrator, OrchestrationExecutionState
from core.conversation.context_builder import ContextBuilder
from core.conversation.persistent_memory import PreparedMemoryContext, TopicMemoryService, diagnostics_to_text
from core.conversation.request_pipeline import RequestPipeline
from core.conversation.request_trace import RequestTraceLogger
from core.conversation.session import ConversationSession
from core.conversation.session_manager import SessionManager
from core.conversation.session_summarizer import SessionSummarizer
from core.documents.models import DocumentSearchConfig, DocumentSearchRoot
from core.application.contracts import ActionSuggestion
from core.llm.ollama_client import OllamaClientError


logger = logging.getLogger(__name__)


def _platform_start_file(path: str) -> None:
    if hasattr(os, "startfile"):
        os.startfile(path)
        return
    raise OSError("open operation is not supported on this platform")


class AssistantCoordinator:
    def __init__(
        self,
        assistant_name: str,
        memory_store: Any,
        config: dict[str, Any],
        ollama_client: LLMClient,
        context_builder: ContextBuilder | None = None,
        session: ConversationSession | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.assistant_name = assistant_name
        self.memory_store = memory_store
        self.config = config
        self.context_builder = context_builder or ContextBuilder(assistant_name, memory_store)
        self.ollama_client = ollama_client
        self.session = session or ConversationSession(max_messages=8)
        self.session_manager = session_manager
        self.summarizer = SessionSummarizer()
        self.request_pipeline = RequestPipeline(config=config)
        self.topic_memory_service: TopicMemoryService | None = None
        self._last_recalled_public_context: dict[str, Any] | None = None
        self._last_general_knowledge_result: dict[str, Any] | None = None
        self._last_file_operations_payload: dict[str, Any] | None = None
        knowledge_config = self.config.get("general_knowledge", {}) if isinstance(self.config, dict) else {}
        self.general_knowledge_router = GeneralKnowledgeRouter(knowledge_config)
        max_iterations = 2
        if isinstance(knowledge_config, dict):
            try:
                max_iterations = max(1, int(knowledge_config.get("orchestrator_max_iterations", 2)))
            except (TypeError, ValueError):
                max_iterations = 2
        self.orchestrator = AssistantOrchestrator(
            self.ollama_client,
            self.general_knowledge_router,
            max_iterations=max_iterations,
            default_weather_location_resolver=self._default_weather_location,
            orchestration_context_provider=self._tracked_orchestration_context,
        )
        audit_path = self.config.get("audit_path") if isinstance(self.config, dict) else None
        if isinstance(audit_path, str):
            audit_path = Path(audit_path)
        self.trace_logger = RequestTraceLogger(audit_path / "request_trace.jsonl" if isinstance(audit_path, Path) else None)

    def respond(self, user_message: str, project_id: str | None = None) -> str:
        session = self._resolve_session()
        self._last_recalled_public_context = None
        self._last_general_knowledge_result = None
        self._last_file_operations_payload = None
        set_active_provider = getattr(self.general_knowledge_router, "set_active_provider", None)
        if callable(set_active_provider):
            set_active_provider(session.metadata.get("active_capability") if hasattr(session, "metadata") else None)
        state = {"project_id": project_id, "session": session}
        request = self.request_pipeline.build_request(user_message, state=state)
        prepared_memory: PreparedMemoryContext | None = None
        trace_payload = {
            "conversation_id": getattr(session, "id", None),
            "session_id": getattr(session, "id", None),
            "raw_user_message": user_message,
            "previous_message_count": len(session.get_messages()),
            "pending_interaction": session.pending_interaction,
            "deterministic_parser_result": {
                "intent": request.intent,
                "requires_tool": request.requires_tool,
                "response_allowed": request.response_allowed,
                "target": request.target,
                "scope": request.scope,
                "filters": request.filters,
            },
            "tracked_data_context": self._tracked_orchestration_context(session),
        }

        if request.requires_tool and request.intent == "read_file":
            target_path = str(request.target.get("path", "")).strip()
            if target_path:
                candidate = Path(target_path).expanduser()
                try:
                    resolved = candidate.resolve()
                except OSError:
                    resolved = candidate
                authorized = self._is_path_authorized(resolved)
                if not authorized:
                    roots = self._candidate_roots()
                    if not roots:
                        response = "No approved file roots are configured."
                    else:
                        response = f"The file could not be read: {resolved} (not in an approved root)"
                    return self._trace_and_persist(
                        trace_payload,
                        user_message,
                        response,
                        selected_tool="read_file",
                        tool_arguments={"path": str(resolved)},
                        tool_result={"status": "error", "path": str(resolved), "reason": "not_authorized"},
                    )
                if resolved.exists() and resolved.is_file():
                    response = self._summarize_file(resolved, user_message, project_id)
                    return self._trace_and_persist(
                        trace_payload,
                        user_message,
                        response,
                        selected_tool="read_file",
                        tool_arguments={"path": str(resolved)},
                        tool_result={"status": "success", "path": str(resolved)},
                    )
                response = f"The file could not be read: {resolved}"
                return self._trace_and_persist(
                    trace_payload,
                    user_message,
                    response,
                    selected_tool="read_file",
                    tool_arguments={"path": str(resolved)},
                    tool_result={"status": "error", "path": str(resolved)},
                )

        if request.requires_tool and request.intent == "count_files":
            root_path = str(request.target.get("path", "")).strip()
            if root_path:
                root = Path(root_path).expanduser()
                try:
                    resolved_root = root.resolve()
                except OSError:
                    resolved_root = root
                extension = self._normalize_extension(str(request.filters.get("extension", ".md") or ".md"))
                recursive = self._parse_bool(request.filters.get("recursive", True), default=True)
                if not self._is_path_authorized(resolved_root):
                    if not self._candidate_roots():
                        response = f"The root could not be scanned: {resolved_root} (no approved roots are configured)"
                    else:
                        response = f"The root could not be scanned: {resolved_root} (not in an approved root)"
                    return self._trace_and_persist(
                        trace_payload,
                        user_message,
                        response,
                        selected_tool="count_files",
                        tool_arguments={"path": str(resolved_root), "extension": extension},
                        tool_result={"status": "error", "path": str(resolved_root), "reason": "not_authorized"},
                    )
                if not resolved_root.exists():
                    response = f"The root could not be scanned: {resolved_root} (does not exist)"
                    return self._trace_and_persist(
                        trace_payload,
                        user_message,
                        response,
                        selected_tool="count_files",
                        tool_arguments={"path": str(resolved_root), "extension": extension},
                        tool_result={"status": "error", "path": str(resolved_root), "reason": "missing_root"},
                    )
                if not resolved_root.is_dir():
                    response = f"The root could not be scanned: {resolved_root} (not a directory)"
                    return self._trace_and_persist(
                        trace_payload,
                        user_message,
                        response,
                        selected_tool="count_files",
                        tool_arguments={"path": str(resolved_root), "extension": extension},
                        tool_result={"status": "error", "path": str(resolved_root), "reason": "not_a_directory"},
                    )
                matches = []
                walk_errors: list[str] = []
                try:
                    for current_root, dirs, files in os.walk(resolved_root, onerror=lambda error: walk_errors.append(str(error))):
                        if not recursive:
                            dirs[:] = []
                        root_config = self._document_search_root_for_path(resolved_root)
                        dirs[:] = [
                            item
                            for item in dirs
                            if root_config is None or not self._document_search_config().is_excluded_directory(
                                root_config,
                                Path(current_root).relative_to(resolved_root) / item,
                                item,
                            )
                        ]
                        for name in files:
                            if self._matches_extension(name, extension):
                                matches.append(os.path.join(current_root, name))
                except PermissionError as error:
                    response = f"The root could not be scanned: {resolved_root} ({error})"
                    return self._trace_and_persist(
                        trace_payload,
                        user_message,
                        response,
                        selected_tool="count_files",
                        tool_arguments={"path": str(resolved_root), "extension": extension},
                        tool_result={"status": "error", "path": str(resolved_root), "reason": str(error)},
                    )
                response = f"Found {len(matches)} {extension} files under {resolved_root}"
                if walk_errors:
                    response = response + f"; {len(walk_errors)} subdirectory access error(s) were encountered."
                return self._trace_and_persist(
                    trace_payload,
                    user_message,
                    response,
                    selected_tool="count_files",
                    tool_arguments={"path": str(resolved_root), "extension": extension},
                    tool_result={
                            "status": "partial" if walk_errors else "success",
                            "count": len(matches),
                            "path": str(resolved_root),
                            "extension": extension,
                            "walk_errors": walk_errors,
                        },
                )

        if request.requires_tool and request.intent == "find_files":
            query = str(request.target.get("query", "")).strip()
            if query:
                ranked_matches: list[dict[str, Any]] = []
                configured_roots = self._candidate_roots()
                searched_roots: list[str] = []
                failed_roots: list[str] = []
                walk_errors_by_root: dict[str, list[str]] = {}
                for root in configured_roots:
                    try:
                        resolved_root = Path(root).expanduser().resolve()
                    except OSError:
                        resolved_root = Path(root).expanduser()
                    if not self._is_path_authorized(resolved_root):
                        failed_roots.append(str(resolved_root))
                        continue
                    if not resolved_root.exists():
                        failed_roots.append(str(resolved_root))
                        continue
                    if not resolved_root.is_dir():
                        failed_roots.append(str(resolved_root))
                        continue
                    searched_roots.append(str(resolved_root))
                    walk_errors: list[str] = []
                    for current_root, dirs, files in os.walk(resolved_root, onerror=lambda error: walk_errors.append(str(error))):
                        root_config = self._document_search_root_for_path(resolved_root)
                        dirs[:] = [
                            item
                            for item in dirs
                            if root_config is None or not self._document_search_config().is_excluded_directory(
                                root_config,
                                Path(current_root).relative_to(resolved_root) / item,
                                item,
                            )
                        ]
                        for name in files:
                            classification = self._classify_match(name, query)
                            if classification is None:
                                continue
                            ranked_matches.append(
                                {
                                    "path": str(Path(current_root) / name),
                                    "classification": classification,
                                }
                            )
                    if walk_errors:
                        walk_errors_by_root[str(resolved_root)] = walk_errors
                classification_order = {
                    "exact": 0,
                    "suffix": 1,
                    "partial": 2,
                }
                ranked_matches.sort(
                    key=lambda item: (
                        classification_order.get(item["classification"], 99),
                        str(item["path"]).lower(),
                    )
                )
                total_matches = len(ranked_matches)
                limit = self._search_result_limit()
                displayed_matches = ranked_matches[:limit]
                requested_action = str(request.target.get("requested_action", "select_file"))
                self._last_file_operations_payload = {
                    "mode": "search",
                    "query": query,
                    "total_matches": total_matches,
                    "displayed_matches": len(displayed_matches),
                    "requested_action": requested_action,
                    "searched_roots": searched_roots,
                    "failed_roots": failed_roots,
                    "walk_errors": walk_errors_by_root,
                    "matches": [
                        {
                            "position": index,
                            "path": str(match["path"]),
                            "classification": str(match.get("classification", "partial")),
                        }
                        for index, match in enumerate(ranked_matches, start=1)
                    ],
                }
                if ranked_matches:
                    results = [
                        {"position": index, "path": match["path"], "classification": match["classification"]}
                        for index, match in enumerate(displayed_matches, start=1)
                    ]
                    session.pending_interaction = {
                        "type": "file_selection",
                        "requested_action": requested_action,
                        "results": results,
                    }
                    session.last_result_set = {
                        "query": query,
                        "total_matches": total_matches,
                        "all_results": self._last_file_operations_payload["matches"],
                        "results": results,
                        "selected_position": None,
                        "requested_action": requested_action,
                    }
                    labels = [f"{item['position']}. {item['path']} ({item['classification']})" for item in results]
                    response = "Found matching files:\n" + "\n".join(labels)
                    if total_matches > limit:
                        response = f"Showing the first {limit} of {total_matches} matching files.\n" + response
                    if requested_action == "open_path":
                        response = response + "\nReply with the number to open the selected file."
                    else:
                        response = response + "\nReply with the number to continue."
                else:
                    if searched_roots:
                        response = f"No match was found in {len(searched_roots)} successfully searched root(s)."
                    else:
                        response = f"No match was found. No configured roots could be searched ({', '.join(failed_roots) if failed_roots else 'unknown'})."
                has_partial_failure = bool(failed_roots or walk_errors_by_root)
                status = "error" if not searched_roots else "partial" if has_partial_failure else "success"
                if has_partial_failure:
                    if failed_roots:
                        response = response + f"\nSome roots or subdirectories could not be searched: {', '.join(failed_roots)}."
                    if walk_errors_by_root:
                        response = response + "\nSome subdirectory access error(s) were encountered during the scan."
                return self._trace_and_persist(
                    trace_payload,
                    user_message,
                    response,
                    selected_tool="find_files",
                    tool_arguments={"query": query},
                    tool_result={"status": status, "matches": [match["path"] for match in displayed_matches], "searched_roots": searched_roots, "failed_roots": failed_roots, "walk_errors": walk_errors_by_root},
                )

        if request.requires_tool and request.intent == "select_pending_result":
            try:
                selection = int(request.target.get("selection", 0))
            except (TypeError, ValueError):
                selection = 0
            pending = session.pending_interaction if hasattr(session, "pending_interaction") else None
            if pending is not None and isinstance(pending, dict):
                results = pending.get("results", []) if isinstance(pending.get("results", []), list) else []
                selected = None
                for item in results:
                    if isinstance(item, dict) and int(item.get("position", 0)) == selection:
                        selected = item
                        break
                if selected is not None:
                    path_value = str(selected.get("path", ""))
                    session.last_selected_file = path_value
                    session.last_tool_result = {"selection": selection, "path": path_value}
                    if session.last_result_set is not None:
                        session.last_result_set["selected_position"] = selection
                    requested_action = str(pending.get("requested_action", "select_file"))
                    if requested_action == "summarize_file":
                        response = self._summarize_file(Path(path_value).expanduser(), user_message, project_id)
                    elif requested_action == "open_path":
                        response = self._open_path(Path(path_value).expanduser())
                    else:
                        response = f"Selected {path_value}"
                    session.pending_interaction = None
                    return self._trace_and_persist(
                        trace_payload,
                        user_message,
                        response,
                        selected_tool="select_pending_result",
                        tool_arguments={"selection": selection},
                        tool_result={"status": "success", "path": path_value},
                    )
            results = pending.get("results", []) if pending is not None and isinstance(pending, dict) and isinstance(pending.get("results", []), list) else []
            response = f"Choose a number from 1 through {len(results)}." if results else "No matching selection was found."
            return self._trace_and_persist(
                trace_payload,
                user_message,
                response,
                selected_tool="select_pending_result",
                tool_arguments={"selection": selection},
                tool_result={"status": "error"},
            )

        if user_message.strip().lower() == "/help":
            help_text = (
                "Available commands:\n"
                "- /help\n"
                "- /index status\n"
                "- /index scan\n"
                "- /index scan <root>\n"
                "- /index errors\n"
                "- /search <query>\n"
                "- /search recent\n"
                "- /topics\n"
                "- /topic <name-or-id>\n"
                "- /conversations\n"
                "- /resume <session-id>\n"
                "- /new"
            )
            return self._trace_and_persist(
                trace_payload,
                user_message,
                help_text,
                selected_tool="help",
                tool_arguments={},
                tool_result={"status": "success"},
            )

        if request.requires_tool and not request.response_allowed:
            response = f"Tool routing failed: unsupported intent {request.intent}"
            return self._trace_and_persist(
                trace_payload,
                user_message,
                response,
                selected_tool=None,
                tool_arguments={},
                tool_result={"status": "error", "reason": "unsupported_intent"},
            )

        self.orchestrator.router = self.general_knowledge_router
        try:
            orchestration_outcome = self.orchestrator.orchestrate(
                user_message,
                session=session,
            )
        except Exception as error:
            # Orchestration runs before the turn is recorded, so preserve the
            # user message and the error event exactly as the generation path does.
            record = self._turn_recorder(session)
            record("user", user_message)
            self._record_generation_failure(record, error)
            raise
        trace_payload["orchestration"] = orchestration_outcome.trace_payload()

        if orchestration_outcome.state == OrchestrationExecutionState.AWAITING_CLARIFICATION and orchestration_outcome.chat_response:
            return self._trace_and_persist(
                trace_payload,
                user_message,
                orchestration_outcome.chat_response,
                selected_tool=None,
                tool_arguments={},
                tool_result={"status": "clarification_required"},
            )

        if orchestration_outcome.state == OrchestrationExecutionState.FAILED and orchestration_outcome.chat_response:
            return self._trace_and_persist(
                trace_payload,
                user_message,
                orchestration_outcome.chat_response,
                selected_tool=None,
                tool_arguments={},
                tool_result={"status": "failed"},
            )

        if orchestration_outcome.state == OrchestrationExecutionState.PARTIALLY_COMPLETED and orchestration_outcome.chat_response:
            return self._trace_and_persist(
                trace_payload,
                user_message,
                orchestration_outcome.chat_response,
                selected_tool=orchestration_outcome.selected_capability,
                tool_arguments={},
                tool_result={"status": "partial"},
            )

        route_result = orchestration_outcome.route_result
        selected_tool: str | None = None
        tool_arguments: dict[str, Any] = {}
        tool_result: dict[str, Any] = {"status": "skipped"}
        if route_result is not None:
            selected_tool = route_result.provider
            if route_result.response is not None:
                if hasattr(session, "metadata"):
                    session.metadata["active_capability"] = route_result.provider
                self._last_general_knowledge_result = {
                    "provider": route_result.provider,
                    "detail_type": getattr(route_result, "detail_type", "text"),
                    "detail_title": getattr(route_result, "detail_title", None),
                    "detail_content": getattr(route_result, "detail_content", None),
                    "metadata": getattr(route_result, "metadata", None) or {},
                }
                capability_response = self._summary_from_capability_facts(for_chat_summary=False)
                if not capability_response:
                    capability_response = str(route_result.response).strip()
                return self._trace_and_persist(
                    trace_payload,
                    user_message,
                    capability_response,
                    selected_tool=selected_tool,
                    tool_arguments=tool_arguments,
                    tool_result={"status": "success"},
                )
            if route_result.fallback_notice is not None:
                tool_result = {"status": "fallback", "reason": route_result.fallback_notice}

        active_session = self.session_manager.get_active_session() if self.session_manager is not None else None
        history = active_session.get_messages() if active_session is not None else session.get_messages()
        recent_limit = self._recent_message_limit()
        recent_history = history[-recent_limit:] if recent_limit > 0 else []
        session_summary = active_session.summary if active_session is not None else None

        persistent_memory_context = ""
        memory_diagnostics: dict[str, Any] | None = None
        try:
            if self.topic_memory_service is not None and self.topic_memory_service.config.enabled and session.id is not None:
                prepared_memory = self.topic_memory_service.prepare_user_turn(
                    conversation_id=session.id,
                    conversation_title=getattr(session, "title", None),
                    user_message=user_message,
                )
                persistent_memory_context = prepared_memory.context_block
                memory_diagnostics = prepared_memory.diagnostics
                public_recall = memory_diagnostics.get("public_recall") if isinstance(memory_diagnostics, dict) else None
                self._last_recalled_public_context = public_recall if isinstance(public_recall, dict) else None
                self._active_topic_id = str(prepared_memory.topic_id)
                self._active_topic_title = prepared_memory.topic_name
                self._log_memory_diagnostics(memory_diagnostics)
        except Exception as error:
            logger.warning("Persistent memory prepare failed: %s", error)

        llm_user_message = self._prepare_user_message_for_llm(user_message)
        system_prompt = self._build_system_prompt(
            llm_user_message,
            project_id,
            persistent_memory_context=persistent_memory_context,
        )

        record = self._turn_recorder(session)
        record("user", user_message)
        try:
            response = self.ollama_client.generate(system_prompt, llm_user_message)
        except Exception as error:
            self._record_generation_failure(record, error)
            raise
        record("assistant", response)
        self._finalize_topic_memory(prepared_memory, user_message, response)
        if active_session is not None:
            self._maybe_update_session_summary()
        trace_payload.update(
            {
                "selected_tool": selected_tool,
                "tool_arguments": tool_arguments,
                "tool_result": tool_result,
                "final_response": response,
            }
        )
        if memory_diagnostics is not None:
            trace_payload["persistent_memory"] = memory_diagnostics
        self.trace_logger.log(trace_payload)
        return response

    def summarize_for_chat(self, user_message: str, detailed_response: str, project_id: str | None = None) -> str:
        _ = project_id
        raw = (detailed_response or "").strip()
        if not raw:
            return ""

        facts_summary = self._summary_from_capability_facts(for_chat_summary=True)
        if facts_summary:
            return facts_summary

        if self._looks_like_self_contained_answer(raw):
            return self._fallback_chat_summary(raw)

        summary_prompt = (
            "User request:\n"
            f"{user_message}\n\n"
            "Detailed response:\n"
            f"{raw}\n\n"
            "Rewrite the detailed response as one brief natural conversational paragraph that fully answers the user on its own. "
            "Include the main conclusion, key values, and important limits or caveats when present. "
            "Use only the information in the detailed response."
        )

        try:
            summary = self.ollama_client.generate(self._summary_system_prompt(), summary_prompt)
        except (OllamaClientError, TimeoutError, OSError, ValueError) as error:
            logger.warning("Falling back to deterministic chat summary: %s", error)
            return self._fallback_chat_summary(raw)

        normalized = self._normalize_chat_summary(summary)
        if not normalized:
            return self._fallback_chat_summary(raw)
        return normalized

    def _summary_from_capability_facts(self, *, for_chat_summary: bool) -> str:
        latest = self._last_general_knowledge_result if isinstance(self._last_general_knowledge_result, dict) else {}
        metadata = latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
        capability = str(metadata.get("capability", "")).strip().lower()
        facts = metadata.get("facts") if isinstance(metadata.get("facts"), dict) else {}
        if not capability or not facts:
            return ""

        if capability == "stocks":
            ticker = str(facts.get("ticker", "")).strip()
            price = facts.get("price")
            change = facts.get("change")
            percent_change = facts.get("percent_change")
            if ticker and isinstance(price, (int, float)):
                summary = f"{ticker} is at ${float(price):.2f}."
                if isinstance(change, (int, float)) and isinstance(percent_change, (int, float)):
                    summary = f"{ticker} is at ${float(price):.2f}, {float(change):+.2f} ({float(percent_change):+.2f}%) for this session."
                if not for_chat_summary:
                    direct = f"{ticker}: ${float(price):.2f}"
                    if isinstance(change, (int, float)) and isinstance(percent_change, (int, float)):
                        direct = f"{direct} ({float(change):+.2f}, {float(percent_change):+.2f}%)"
                    return direct
                return summary

        if capability == "time":
            timezone_value = str(facts.get("timezone", "")).strip()
            date_time = str(facts.get("datetime", "")).strip()
            if timezone_value and date_time:
                if for_chat_summary:
                    return f"Current time in {timezone_value}: {date_time}."
                return f"Current time in {timezone_value}: {date_time}"

        if capability == "lookup":
            answer = str(facts.get("answer", "")).strip()
            if answer:
                return answer

        if capability == "news":
            headlines = facts.get("headlines") if isinstance(facts.get("headlines"), list) else []
            normalized = [str(item).strip() for item in headlines if str(item).strip()]
            if normalized:
                if len(normalized) == 1:
                    return f"Top headline: {normalized[0]}."
                if not for_chat_summary:
                    return "Top headlines: " + "; ".join(normalized[:2])
                return f"Top headlines include {normalized[0]} and {normalized[1]}."

        if capability == "weather":
            range_name = str(facts.get("range", "")).strip().lower()
            detail_items = facts.get("details") if isinstance(facts.get("details"), list) else []
            detail_lines = [str(item).strip() for item in detail_items if str(item).strip()]

            def _weather_daily_lines(lines: list[str]) -> list[str]:
                normalized: list[str] = []
                for raw in lines:
                    text = raw.lstrip("- ").strip()
                    lowered = text.lower()
                    if not text:
                        continue
                    if lowered.startswith("currently available forecast window"):
                        continue
                    if lowered.startswith("daily outlook"):
                        continue
                    if lowered.startswith("coverage note:"):
                        continue
                    normalized.append(text)
                return normalized

            if range_name == "next_week":
                daily_lines = _weather_daily_lines(detail_lines)
                if daily_lines:
                    if for_chat_summary:
                        return "Next week's forecast is not available from this source yet. Available outlook: " + " ".join(daily_lines[:3])
                    return "Next week's forecast is not available from this source yet. Available outlook: " + " ".join(daily_lines[:3])

            if range_name in {"week", "weekend"}:
                daily_lines = _weather_daily_lines(detail_lines)
                if daily_lines:
                    label = "rest of the week" if range_name == "week" else "weekend"
                    if for_chat_summary:
                        return f"Here is the {label}: " + " ".join(daily_lines[:3])
                    return f"Here is the {label}: " + " ".join(daily_lines[:3])

            if range_name == "today":
                location = str(facts.get("location", "")).strip()
                condition = str(facts.get("condition", "")).strip()
                temp = str(facts.get("current_temp_f", "")).strip()
                headline = str(facts.get("headline", "")).strip()
                rain = str(facts.get("rain_summary", "")).strip()
                summary_parts: list[str] = []
                if condition and temp:
                    if location and location != "default":
                        summary_parts.append(f"Current weather in {location}: {condition}, {temp}.")
                    else:
                        summary_parts.append(f"Current weather: {condition}, {temp}.")
                if headline:
                    summary_parts.append(f"Outlook: {headline}")
                if rain:
                    summary_parts.append(rain)
                if summary_parts:
                    merged = " ".join(summary_parts).strip()
                    if not for_chat_summary and merged.endswith("."):
                        return merged[:-1]
                    return merged

            if range_name and range_name != "current":
                return ""
            location = str(facts.get("location", "")).strip()
            condition = str(facts.get("condition", "")).strip()
            temp = str(facts.get("current_temp_f", "")).strip()
            rain = str(facts.get("rain_summary", "")).strip()
            if condition and temp:
                if location and location != "default":
                    summary = f"Current weather in {location}: {condition}, {temp}."
                else:
                    summary = f"Current weather: {condition}, {temp}."
                if rain:
                    summary = f"{summary} {rain}"
                if not for_chat_summary and summary.endswith("."):
                    return summary[:-1]
                return summary

        return ""

    def suggest_follow_up_actions(
        self,
        user_message: str,
        detailed_response: str,
        topic_title: str | None = None,
        *,
        max_actions: int = 4,
    ) -> list[ActionSuggestion]:
        raw = (detailed_response or "").strip()
        if not raw:
            return []
        if self._looks_like_clarification_response(raw):
            return []

        prompt = (
            "Generate concise follow-up prompts for continued exploration of the same topic.\n"
            "Return JSON only as an array of objects.\n"
            "Each object must contain keys: label, prompt.\n"
            f"Return at most {max_actions} objects.\n"
            "Each prompt must be phrased as something the user would send to the assistant next, not as a question from the assistant to the user.\n"
            "Do not ask the user to clarify, confirm, share, or provide more information.\n"
            "Do not include markdown code fences.\n\n"
            f"Topic title: {topic_title or 'General'}\n"
            f"User request: {user_message}\n\n"
            f"Detailed response:\n{raw}"
        )
        try:
            payload = self.ollama_client.generate(self._follow_up_system_prompt(), prompt)
        except (OllamaClientError, TimeoutError, OSError, ValueError) as error:
            logger.warning("Failed to generate follow-up actions: %s", error)
            return []

        data = self._parse_json_array(payload)
        if not isinstance(data, list):
            return []

        actions: list[ActionSuggestion] = []
        seen: set[str] = set()
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            prompt_text = str(item.get("prompt", "")).strip()
            if not label or not prompt_text:
                continue
            if label.lower() in seen or prompt_text.lower() in seen:
                continue
            seen.add(label.lower())
            seen.add(prompt_text.lower())
            actions.append(
                ActionSuggestion(
                    id=f"follow-up-{index}",
                    label=label,
                    payload={"command": prompt_text},
                )
            )
            if len(actions) >= max_actions:
                break
        return actions

    def _generate_topic_patch(self, prepared: PreparedMemoryContext, user_message: str, assistant_response: str) -> dict[str, Any] | None:
        if self.topic_memory_service is None:
            return None
        memory_cfg = self.config.get("memory", {}) if isinstance(self.config, dict) else {}
        llm_patch_enabled = bool(memory_cfg.get("generate_topic_patch_with_llm", False)) if isinstance(memory_cfg, dict) else False
        if not llm_patch_enabled:
            return None
        current_recall = self.topic_memory_service.build_public_recall(prepared.topic_id, [])
        current_topic = current_recall.get("current_topic") if isinstance(current_recall, dict) else {}
        if not isinstance(current_topic, dict):
            current_topic = {}

        prompt = (
            "Generate a canonical topic patch as JSON only.\n"
            "Return an object with keys: expected_version, operations.\n"
            "Do not include markdown or code fences.\n"
            "Allowed operations: set_topic_title, set_goal, add_requirement, update_requirement, remove_requirement, add_item, update_item, set_item_attribute, remove_item_attribute, reject_item, restore_item, remove_item, add_decision, replace_decision, remove_decision, add_open_question, resolve_open_question, add_note, remove_note.\n"
            "If no update is needed, return operations as an empty array.\n\n"
            f"Topic id: {prepared.topic_id}\n"
            f"Expected version: {prepared.expected_version}\n"
            f"Current state: {json.dumps(current_topic, ensure_ascii=True)}\n\n"
            f"User message: {user_message}\n\n"
            f"Assistant response: {assistant_response}"
        )

        try:
            payload = self.ollama_client.generate(self._topic_patch_system_prompt(), prompt)
        except (OllamaClientError, TimeoutError, OSError, ValueError) as error:
            logger.warning("Topic patch generation failed: %s", error)
            return None

        patch = self._parse_json_object(payload)
        if not isinstance(patch, dict):
            return None
        operations = patch.get("operations")
        if not isinstance(operations, list):
            return None
        expected_version = patch.get("expected_version")
        if not isinstance(expected_version, int):
            expected_version = prepared.expected_version
        return {
            "expected_version": expected_version,
            "operations": [item for item in operations if isinstance(item, dict)][:100],
        }

    def _topic_patch_system_prompt(self) -> str:
        return (
            "You generate validated patch operations for canonical topic state updates.\n"
            "Use only provided topic state and messages.\n"
            "Do not invent facts.\n"
            "Return JSON only as an object with expected_version and operations."
        )

    def _normalize_chat_summary(self, summary: str) -> str:
        normalized = (summary or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return ""
        lines = []
        for line in normalized.split("\n"):
            candidate = line.strip()
            if not candidate:
                continue
            candidate = re.sub(r"^#{1,6}\s*", "", candidate)
            candidate = re.sub(r"^[-*•]\s+", "", candidate)
            candidate = candidate.replace("**", "").replace("__", "")
            candidate = candidate.replace("*", "").replace("_", "")
            lines.append(candidate)
        normalized = " ".join(lines).strip() if lines else ""
        normalized = re.sub(r"\s+", " ", normalized).strip()
        normalized = self._trim_incomplete_summary(normalized)
        if len(normalized) > 800:
            trimmed = normalized[:797].rsplit(" ", 1)[0]
            normalized = f"{trimmed}..." if trimmed else normalized[:800]
        return normalized

    def _fallback_chat_summary(self, detailed_response: str) -> str:
        normalized = re.sub(r"\s+", " ", (detailed_response or "").strip())
        if not normalized:
            return ""
        sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", normalized) if item.strip()]
        complete = [sentence for sentence in sentences if re.search(r"[.!?]$", sentence)]
        if complete:
            candidate = " ".join(complete[:2]).strip()
            candidate = self._trim_incomplete_summary(candidate)
            if candidate:
                if len(candidate) > 400:
                    trimmed = candidate[:397].rsplit(" ", 1)[0]
                    candidate = f"{trimmed}..." if trimmed else candidate[:400]
                return candidate
        compact = self._normalize_chat_summary(detailed_response)
        if compact:
            if len(compact) > 400:
                trimmed = compact[:397].rsplit(" ", 1)[0]
                return f"{trimmed}..." if trimmed else compact[:400]
            return compact
        return normalized[:400].rstrip()

    def _looks_like_self_contained_answer(self, text: str) -> bool:
        normalized = re.sub(r"\s+", " ", (text or "").strip())
        if not normalized:
            return False
        indicators = (
            "current weather",
            "rain outlook",
            "forecast",
            "today:",
            "tomorrow:",
            "provider coverage",
            "i could not retrieve",
        )
        return any(token in normalized.lower() for token in indicators)

    def _summary_system_prompt(self) -> str:
        return (
            "You rewrite assistant answers into concise conversational summaries.\n"
            "Use only the supplied detailed response and user request.\n"
            "The summary must be independently useful even if no details pane is visible.\n"
            "Include the main result, important values, and any material caveats or provider limits.\n"
            "Do not refer to a details pane, further details, sections, attachments, or hidden context.\n"
            "Do not add outside facts, assumptions, or prior-context information.\n"
            "Return exactly one natural paragraph with no headings, bullets, markdown, labels, or JSON."
        )

    def _follow_up_system_prompt(self) -> str:
        return (
            "You produce follow-up prompts for an assistant details pane.\n"
            "Use only the supplied detailed response.\n"
            "Each prompt should keep the user on the same topic.\n"
            "Each prompt must be written as a user request to the assistant, not as a question directed back to the user.\n"
            "Do not generate clarification questions or prompts that ask the user to provide missing information.\n"
            "Do not invent facts.\n"
            "Return only JSON."
        )

    def _looks_like_clarification_response(self, text: str) -> bool:
        normalized = re.sub(r"\s+", " ", (text or "").strip()).lower()
        if not normalized:
            return False
        markers = (
            "could you clarify",
            "can you share",
            "let me know which",
            "which specific",
            "i need more details",
            "if you're referring to",
            "could you let me know",
        )
        if any(marker in normalized for marker in markers):
            return True
        return normalized.endswith("?")

    def _parse_json_array(self, text: str) -> list[dict[str, object]] | None:
        payload = (text or "").strip()
        if not payload:
            return None
        fenced = re.search(r"```(?:json)?\s*(\[.*\])\s*```", payload, flags=re.DOTALL)
        if fenced:
            payload = fenced.group(1)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            return parsed
        return None

    def _parse_json_object(self, text: str) -> dict[str, Any] | None:
        payload = (text or "").strip()
        if not payload:
            return None
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", payload, flags=re.DOTALL)
        if fenced:
            payload = fenced.group(1)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
        return None

    def _trim_incomplete_summary(self, text: str) -> str:
        candidate = (text or "").strip()
        while candidate.endswith((":", ",", ";", "-")):
            candidate = candidate[:-1].rstrip()
        return candidate

    def _build_system_prompt(
        self,
        user_message: str,
        project_id: str | None = None,
        persistent_memory_context: str | None = None,
    ) -> str:
        session = self._resolve_session()
        active_session = self.session_manager.get_active_session() if self.session_manager is not None else None
        history = active_session.get_messages() if active_session is not None else session.get_messages()
        recent_limit = self._recent_message_limit()
        recent_history = history[-recent_limit:] if recent_limit > 0 else []
        session_summary = active_session.summary if active_session is not None else None
        context = self.context_builder.build_context(
            user_message,
            project_id=project_id,
            session_summary=session_summary,
            recent_messages=recent_history,
        )
        memory_block = (persistent_memory_context or "").strip()
        if memory_block:
            context = context + "\n\n" + memory_block
        return (
            f"You are {self.assistant_name}, a personal assistant.\n\n"
            "Default to the most likely ordinary-language interpretation when one meaning is clearly dominant.\n"
            "Ask for clarification only when multiple interpretations are genuinely plausible.\n"
            "Answer the likely request first; mention ambiguity afterward only when still useful.\n"
            "When persistent memory context includes prior recommendations, configurations, or earlier discussed models, treat that context as part of the active conversation and build on it directly.\n"
            "If the user refers to earlier recommendations with phrases like those, earlier, previous, or recommended, use the recalled models and configuration details from persistent memory instead of asking the user to repeat them.\n"
            "When answering from recalled conversation context, explicitly restate the relevant prior models, configurations, or decisions before giving the new answer so the user can see what was recalled.\n"
            "If the recalled context contains exact earlier model names but no live pricing data, say which earlier models were recalled and then explain that current pricing is unavailable rather than claiming the prior models are unknown.\n"
            "Only ask for clarification if the recalled context still does not contain the specific detail needed to answer.\n"
            "Use the supplied user context only when it materially helps answer the current request.\n"
            "Do not introduce the user's location, profession, projects, or preferences into unrelated answers.\n"
            "Silently correct obvious ordinary-language spelling mistakes when intent is clear.\n"
            "Do not autocorrect file paths, filenames, commands, identifiers, code, or quoted text.\n"
            "Do not claim to know information that is not present.\n"
            "Follow the user's stated working preferences.\n"
            "Ask for clarification when a required detail is missing.\n\n"
            f"User context:\n{context}"
        )

    def _prepare_user_message_for_llm(self, user_message: str) -> str:
        text = user_message or ""
        if not text.strip():
            return text
        if text.lstrip().startswith("/"):
            return text

        corrections = self._autocorrect_common_terms()
        if not corrections:
            return text

        quoted_spans = self._quoted_spans(text)
        rebuilt: list[str] = []
        cursor = 0
        for match in re.finditer(r"[A-Za-z]{4,}", text):
            start, end = match.span()
            token = match.group(0)
            if self._span_within_any(start, end, quoted_spans) or self._is_protected_token_context(text, start, end):
                continue
            replacement = corrections.get(token.lower())
            if replacement is None or replacement == token.lower():
                continue
            rebuilt.append(text[cursor:start])
            rebuilt.append(self._apply_case_pattern(token, replacement))
            cursor = end
        if not rebuilt:
            return text
        rebuilt.append(text[cursor:])
        return "".join(rebuilt)

    def _autocorrect_common_terms(self) -> dict[str, str]:
        configured = self.config.get("autocorrect_common_terms", {}) if isinstance(self.config, dict) else {}
        normalized_configured: dict[str, str] = {}
        if isinstance(configured, dict):
            for key, value in configured.items():
                key_text = str(key).strip().lower()
                value_text = str(value).strip().lower()
                if key_text and value_text:
                    normalized_configured[key_text] = value_text

        defaults = {
            "playpus": "platypus",
        }
        defaults.update(normalized_configured)
        return defaults

    def _quoted_spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for match in re.finditer(r"`[^`]*`|\"[^\"]*\"|'[^']*'", text):
            spans.append(match.span())
        return spans

    def _span_within_any(self, start: int, end: int, spans: list[tuple[int, int]]) -> bool:
        for span_start, span_end in spans:
            if start >= span_start and end <= span_end:
                return True
        return False

    def _is_protected_token_context(self, text: str, start: int, end: int) -> bool:
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if (before and before in "._-/\\:") or (after and after in "._-/\\:"):
            return True

        window_start = max(0, start - 12)
        window_end = min(len(text), end + 12)
        window = text[window_start:window_end]
        if re.search(r"[A-Za-z]:\\|/|\\|--[A-Za-z0-9_-]+", window):
            return True
        return False

    def _apply_case_pattern(self, source: str, target: str) -> str:
        if source.isupper():
            return target.upper()
        if source.istitle():
            return target.capitalize()
        return target

    def _trace_and_persist(
        self,
        trace_payload: dict[str, Any],
        user_message: str,
        response: str,
        *,
        selected_tool: str | None,
        tool_arguments: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> str:
        """Close out a turn: finish its trace record, log it, and persist the exchange."""
        trace_payload.update(
            {
                "selected_tool": selected_tool,
                "tool_arguments": tool_arguments,
                "tool_result": tool_result,
                "final_response": response,
            }
        )
        self.trace_logger.log(trace_payload)
        return self._persist_and_return(user_message, response)

    def _turn_recorder(self, session: ConversationSession) -> Callable[..., None]:
        """Return the add_message callable for whichever store owns this turn."""
        if self.session_manager is not None and self.session_manager.get_active_session() is not None:
            return self.session_manager.add_message
        return session.add_message

    def _record_generation_failure(self, record: Callable[..., None], error: Exception) -> None:
        record(
            "system",
            "Assistant response failed.",
            {"error_type": error.__class__.__name__, "error": str(error)},
        )

    def _finalize_topic_memory(self, prepared_memory: PreparedMemoryContext | None, user_message: str, response: str) -> None:
        if prepared_memory is None or self.topic_memory_service is None:
            return
        try:
            topic_patch = self._generate_topic_patch(prepared_memory, user_message, response)
            finalized_public_recall = self.topic_memory_service.finalize_assistant_turn(prepared_memory, response, topic_patch=topic_patch)
            if isinstance(finalized_public_recall, dict):
                self._last_recalled_public_context = finalized_public_recall
        except Exception as error:
            logger.warning("Persistent memory finalize failed: %s", error)

    def _maybe_update_session_summary(self) -> None:
        if self.session_manager is None:
            return
        refreshed_session = self.session_manager.get_active_session()
        if refreshed_session is None:
            return
        trigger = self._summary_trigger_message_count()
        message_count = len(refreshed_session.get_messages())
        last_summarized = int(refreshed_session.metadata.get("last_summarized_message_count", 0))
        if trigger <= 0 or message_count < last_summarized + trigger:
            return
        new_messages = refreshed_session.get_messages()[last_summarized:message_count]
        refreshed_session.summary = self.summarizer.summarize(refreshed_session.summary, new_messages)
        refreshed_session.metadata["last_summarized_message_count"] = message_count
        self.session_manager.save_active_session()

    def _persist_and_return(self, user_message: str, content: str) -> str:
        session = self._resolve_session()
        self._persist_topic_memory_turn(session, user_message, content)
        if self.session_manager is not None:
            self.session_manager.add_message("user", user_message)
            self.session_manager.add_message("assistant", content)
            self.session_manager.save_active_session()
            return content

        session.add_message("user", user_message)
        session.add_message("assistant", content)
        return content

    def _persist_topic_memory_turn(self, session: ConversationSession, user_message: str, content: str) -> None:
        if (
            self.topic_memory_service is None
            or not self.topic_memory_service.config.enabled
            or session.id is None
        ):
            return
        try:
            prepared = self.topic_memory_service.prepare_user_turn(
                conversation_id=session.id,
                conversation_title=getattr(session, "title", None),
                user_message=user_message,
            )
            topic_patch = self._generate_topic_patch(prepared, user_message, content)
            finalized_public_recall = self.topic_memory_service.finalize_assistant_turn(prepared, content, topic_patch=topic_patch)
            if isinstance(finalized_public_recall, dict):
                self._last_recalled_public_context = finalized_public_recall
            self._active_topic_id = str(prepared.topic_id)
            self._active_topic_title = prepared.topic_name
            self._log_memory_diagnostics(prepared.diagnostics)
        except Exception as error:
            logger.warning("Persistent memory turn persistence failed: %s", error)

    def _log_memory_diagnostics(self, payload: dict[str, Any]) -> None:
        memory_cfg = self.config.get("memory", {}) if isinstance(self.config, dict) else {}
        diagnostics_enabled = bool(memory_cfg.get("diagnostics", False)) if isinstance(memory_cfg, dict) else False
        logger.info("memory_diagnostics=%s", json.dumps(payload, ensure_ascii=True))
        if diagnostics_enabled:
            logger.info("memory_diagnostics_text=%s", diagnostics_to_text(payload))

    def _default_weather_location(self) -> str:
        profile_source = None
        getter = getattr(self.memory_store, "get_profile", None)
        if callable(getter):
            try:
                profile_source = getter()
            except Exception:
                profile_source = None
        if not isinstance(profile_source, dict):
            return ""
        profile = profile_source.get("profile", {}) if isinstance(profile_source.get("profile", {}), dict) else {}
        location = profile.get("location", {}) if isinstance(profile.get("location", {}), dict) else {}
        city = str(location.get("city", "")).strip()
        region = str(location.get("region", "")).strip() or str(location.get("state", "")).strip()
        country = str(location.get("country", "")).strip()
        parts = [item for item in [city, region, country] if item]
        return ", ".join(parts)

    def _tracked_orchestration_context(self, session: ConversationSession) -> dict[str, Any]:
        known_defaults: dict[str, Any] = {}
        known_entities: dict[str, Any] = {}

        default_location = self._default_weather_location()
        if default_location:
            known_defaults["weather_location"] = default_location

        if hasattr(session, "metadata") and isinstance(session.metadata, dict):
            entities = session.metadata.get("entities")
            if isinstance(entities, dict):
                known_entities = entities

        payload: dict[str, Any] = {
            "known_defaults": known_defaults,
            "known_entities": known_entities,
        }
        return payload

    def _resolve_session(self) -> ConversationSession:
        if self.session_manager is not None:
            active_session = self.session_manager.get_active_session()
            if active_session is not None:
                return active_session
        return self.session

    def _is_path_authorized(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        roots = self._candidate_roots()
        if not roots:
            return False
        for root in roots:
            try:
                candidate_root = Path(root).expanduser().resolve()
            except OSError:
                candidate_root = Path(root).expanduser()
            try:
                resolved.relative_to(candidate_root)
                return True
            except ValueError:
                continue
        return False

    def _summarize_file(self, path: Path, user_message: str, project_id: str | None = None) -> str:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser()
        if not self._is_path_authorized(resolved):
            if not self._candidate_roots():
                return "No approved file roots are configured."
            return f"The file could not be read: {resolved} (not in an approved root)"
        if not resolved.exists() or not resolved.is_file():
            return f"The file could not be read: {resolved}"
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return f"The file could not be read: {resolved} ({error})"
        max_chars = self._max_file_chars()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n[truncated due to size limit]"
        prompt = (
            "Read the file content and provide a concise summary of it.\n\n"
            "The file may be truncated; if so, clearly state that the summary covers only the visible portion.\n\n"
            f"File path: {resolved}\n\n"
            f"Content:\n{content}"
        )
        return self.ollama_client.generate(self._build_system_prompt(user_message, project_id), prompt)

    def _open_path(self, path: Path) -> str:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser()
        if not self._is_path_authorized(resolved):
            if not self._candidate_roots():
                return "No approved file roots are configured."
            return f"The file could not be opened: {resolved} (not in an approved root)"
        if not resolved.exists() or not resolved.is_file():
            return f"The file could not be opened: {resolved}"
        try:
            _platform_start_file(str(resolved))
        except Exception as error:
            return f"The file could not be opened: {resolved} ({error})"
        return f"Opened {resolved}"

    def _matches_extension(self, name: str, extension: str) -> bool:
        if not extension:
            return True
        lowered_name = name.lower()
        lowered_extension = extension.lower()
        return lowered_name.endswith(lowered_extension)

    def _normalize_extension(self, extension: str) -> str:
        candidate = (extension or "").strip()
        if not candidate:
            return ".md"
        candidate = candidate.replace("/", "").replace("\\", "")
        if candidate.startswith("*"):
            candidate = candidate[1:]
        if candidate and not candidate.startswith("."):
            candidate = f".{candidate}"
        if candidate in {".", ".."}:
            return ".md"
        return candidate

    def _parse_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y"}:
                return True
            if lowered in {"false", "0", "no", "n"}:
                return False
        if isinstance(value, int):
            return value != 0
        return default

    def _classify_match(self, filename: str, query: str) -> str | None:
        lowered_name = filename.lower()
        lowered_query = query.lower().strip()
        if not lowered_query:
            return None
        stem = Path(lowered_name).stem.lower()
        if lowered_name == lowered_query or stem == lowered_query:
            return "exact"
        if lowered_name.endswith(lowered_query):
            return "suffix"
        if lowered_query in lowered_name:
            return "partial"
        return None

    def _search_result_limit(self) -> int:
        configured = self.config.get("search_result_limit") if isinstance(self.config, dict) else None
        if isinstance(configured, int) and configured > 0:
            return configured
        return 25

    def _excluded_directories(self) -> set[str]:
        document_search = self.config.get("document_search") if isinstance(self.config, dict) else None
        if isinstance(document_search, dict):
            configured = document_search.get("excluded_directories")
            if isinstance(configured, (list, set)):
                normalized = {str(item).lower() for item in configured if str(item).strip()}
                if normalized:
                    return normalized
        return {".git", ".venv", "node_modules", "__pycache__", "bin", "obj"}

    def _excluded_directory_prefixes(self) -> set[str]:
        document_search = self.config.get("document_search") if isinstance(self.config, dict) else None
        if isinstance(document_search, dict):
            configured = document_search.get("excluded_directory_prefixes")
            if isinstance(configured, (list, set)):
                normalized = {str(item).lower() for item in configured if str(item).strip()}
                if normalized:
                    return normalized
                return set()
        return {"."}

    def _max_file_chars(self) -> int:
        configured = self.config.get("max_file_chars") if isinstance(self.config, dict) else None
        if isinstance(configured, int) and configured > 0:
            return configured
        return 20000

    def _candidate_roots(self) -> list[str]:
        roots: list[str] = []
        configured = self.config.get("roots") if isinstance(self.config, dict) else None
        if isinstance(configured, list):
            for item in configured:
                if isinstance(item, dict):
                    path_value = str(item.get("path", "")).strip()
                    if path_value:
                        roots.append(path_value)
                elif isinstance(item, str) and item.strip():
                    roots.append(item)

        document_search = self.config.get("document_search") if isinstance(self.config, dict) else None
        if isinstance(document_search, dict):
            configured_roots = document_search.get("roots")
            if isinstance(configured_roots, list):
                for item in configured_roots:
                    if isinstance(item, DocumentSearchRoot):
                        roots.append(str(item.path))
                    elif isinstance(item, dict):
                        path_value = str(item.get("path", "")).strip()
                        if path_value:
                            roots.append(path_value)
                    elif isinstance(item, str) and item.strip():
                        roots.append(item)

        if not roots:
            return []
        return roots

    def _document_search_config(self) -> DocumentSearchConfig:
        document_search = self.config.get("document_search") if isinstance(self.config, dict) else {}
        if not isinstance(document_search, dict):
            document_search = {}
        return DocumentSearchConfig(
            roots=list(document_search.get("roots", [])),
            directory_groups={str(key): set(value) for key, value in document_search.get("directory_groups", {}).items()},
            excluded_directories=set(document_search.get("excluded_directories", [])),
            excluded_directory_prefixes=set(document_search.get("excluded_directory_prefixes", [])),
            supported_extensions=set(),
            max_file_size_mb=1,
        )

    def _document_search_root_for_path(self, root_path: Path) -> DocumentSearchRoot | None:
        for configured_root in self._document_search_config().roots:
            if str(configured_root.path).lower() == str(root_path).lower():
                return configured_root
        return None

    def _conversation_config(self) -> dict[str, Any]:
        conversation = self.config.get("conversation") if isinstance(self.config, dict) else None
        if isinstance(conversation, dict):
            return conversation
        return {}

    def _recent_message_limit(self) -> int:
        configured = self._conversation_config().get("recent_message_limit", 4)
        if isinstance(configured, int) and configured > 0:
            return configured
        return 4

    def _summary_trigger_message_count(self) -> int:
        configured = self._conversation_config().get("summary_trigger_message_count", 24)
        if isinstance(configured, int) and configured > 0:
            return configured
        return 24


