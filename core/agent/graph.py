# File: core/agent/graph.py

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from core.actions.models import ActionRequest
from core.assistant.request_kinds import is_code_request, is_small_talk
from core.llm.models import ChatMessage, LLMRequest, LLMResponse, ToolSpec
from core.llm.ollama_client import OllamaClientError
from core.permissions.models import PermissionLevel, PermissionRequest
from core.permissions.targets import hosts_in, paths_in
from core.results.models import to_json_list
from core.tools.models import ToolArgumentError, ToolKind

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3

TOOL_GUIDANCE = (
    "You can call tools. Call one only when the message asks for something a tool does, with arguments taken "
    "from the message. For conversation, questions, opinions, and requests to write or explain code or text, "
    "answer directly and call no tool. Never invent paths, names, or values the user did not give."
)


class AgentState(TypedDict, total=False):
    user_message: str
    project_id: str | None
    kind: str
    parser: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    capability: dict[str, Any] | None
    answer: str
    iterations: int
    resolved_by: str
    selected_tool: str | None
    tool_arguments: dict[str, Any]
    tool_status: dict[str, Any]
    awaiting: dict[str, Any] | None


@dataclass
class AgentResult:
    text: str
    resolved_by: str = "model"
    selected_tool: str | None = None
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    tool_status: dict[str, Any] = field(default_factory=dict)
    capability: dict[str, Any] | None = None
    awaiting: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def awaiting_confirmation(self) -> bool:
        return self.awaiting is not None


def capability_tool_specs(router: Any) -> tuple[ToolSpec, ...]:
    definitions = getattr(router, "capability_definitions", None)
    if not callable(definitions):
        return ()
    specs: list[ToolSpec] = []
    for item in definitions():
        schema = dict(getattr(item, "request_schema", None) or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        specs.append(ToolSpec(name=str(item.name), description=str(item.description), parameters=schema))
    return tuple(specs)


class IrisAgent:
    def __init__(
        self,
        coordinator: Any,
        *,
        tool_registry: Any | None = None,
        action_executor: Any | None = None,
        knowledge_router: Any | None = None,
        checkpoint_path: str | Path | None = None,
        on_tool_success: Callable[[str, str, dict[str, Any]], None] | None = None,
        tool_auditor: Any | None = None,
        permissions: Any | None = None,
        max_iterations: int = MAX_ITERATIONS,
        on_tool_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.tool_registry = tool_registry
        self.action_executor = action_executor
        self.knowledge_router = knowledge_router
        self.on_tool_success = on_tool_success
        self.on_tool_event = on_tool_event
        self.tool_auditor = tool_auditor
        self.permissions = permissions
        self.max_iterations = max(1, int(max_iterations))
        self._lock = threading.RLock()
        self._saver_context: Any = None
        if checkpoint_path is not None:
            Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
            self._saver_context = SqliteSaver.from_conn_string(str(checkpoint_path))
            self.checkpointer = self._saver_context.__enter__()
        else:
            self.checkpointer = InMemorySaver()
        self.graph = self._build()
        self._turn: dict[str, Any] = {}

    def close(self) -> None:
        if self._saver_context is not None:
            try:
                self._saver_context.__exit__(None, None, None)
            except (OSError, ValueError, RuntimeError, TypeError):
                pass
            self._saver_context = None

    def _build(self) -> Any:
        builder: StateGraph = StateGraph(AgentState)
        builder.add_node("classify", self._classify)
        builder.add_node("file_ops", self._file_ops)
        builder.add_node("plan", self._plan)
        builder.add_node("act", self._act)
        builder.add_node("answer", self._answer)
        builder.add_edge(START, "classify")
        builder.add_conditional_edges("classify", self._after_classify, {"file_ops": "file_ops", "plan": "plan", "answer": "answer"})
        builder.add_edge("file_ops", END)
        builder.add_conditional_edges("plan", self._after_plan, {"act": "act", "end": END, "answer": "answer"})
        builder.add_conditional_edges("act", self._after_act, {"plan": "plan", "answer": "answer", "end": END})
        builder.add_edge("answer", END)
        return builder.compile(checkpointer=self.checkpointer)

    def run(
        self,
        user_message: str,
        *,
        session_id: str,
        project_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AgentResult:
        with self._lock:
            self._turn = {"on_delta": on_delta, "cancel_event": cancel_event}
            state: AgentState = {
                "user_message": user_message,
                "project_id": project_id,
                "kind": "",
                "parser": {},
                "tool_calls": [],
                "tool_results": [],
                "capability": None,
                "iterations": 0,
                "awaiting": None,
                "answer": "",
                "resolved_by": "",
                "selected_tool": None,
                "tool_arguments": {},
                "tool_status": {},
            }
            return self._drive(state, session_id, on_delta)

    def awaiting(self, session_id: str) -> dict[str, Any] | None:
        snapshot = self.graph.get_state(self._config(session_id))
        for task in getattr(snapshot, "tasks", ()) or ():
            for pending in getattr(task, "interrupts", ()) or ():
                value = getattr(pending, "value", None)
                if isinstance(value, dict):
                    return value
        return None

    def resume(self, session_id: str, approved: bool, *, on_delta: Callable[[str], None] | None = None) -> AgentResult | None:
        with self._lock:
            if self.awaiting(session_id) is None:
                return None
            self._turn = {"on_delta": on_delta, "cancel_event": None}
            return self._drive(Command(resume=bool(approved)), session_id, on_delta)

    def _drive(self, payload: Any, session_id: str, on_delta: Callable[[str], None] | None) -> AgentResult:
        config = self._config(session_id)
        final: dict[str, Any] = {}
        for mode, event in self.graph.stream(payload, config, stream_mode=["custom", "values"]):
            if mode == "custom" and isinstance(event, dict) and event.get("delta") and on_delta is not None:
                on_delta(str(event["delta"]))
            elif mode == "values" and isinstance(event, dict):
                final = event
        pending = self.awaiting(session_id)
        return AgentResult(
            text=str(final.get("answer") or (pending or {}).get("message") or ""),
            resolved_by=str(final.get("resolved_by") or ("confirmation" if pending else "model")),
            selected_tool=final.get("selected_tool"),
            tool_arguments=dict(final.get("tool_arguments") or {}),
            tool_status=dict(final.get("tool_status") or {}),
            capability=final.get("capability"),
            awaiting=pending,
            tool_results=list(final.get("tool_results") or []),
        )

    @staticmethod
    def _config(session_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"session-{session_id}"}}

    def _classify(self, state: AgentState) -> dict[str, Any]:
        text = state["user_message"]
        parser = self.coordinator.parse_request(text)
        if parser.get("requires_tool") and parser.get("intent") in {"read_file", "count_files", "find_files", "select_pending_result"}:
            return {"kind": "file_op", "parser": parser}
        if self.coordinator.is_help_request(text):
            return {"kind": "file_op", "parser": parser}
        if is_small_talk(text):
            return {"kind": "small_talk", "parser": parser}
        if is_code_request(text):
            return {"kind": "code", "parser": parser}
        return {"kind": "general", "parser": parser}

    @staticmethod
    def _after_classify(state: AgentState) -> str:
        kind = state.get("kind")
        if kind == "file_op":
            return "file_ops"
        if kind in {"small_talk", "code"}:
            return "answer"
        return "plan"

    def _file_ops(self, state: AgentState) -> dict[str, Any]:
        text = self.coordinator.handle_file_operation(state["user_message"], state.get("parser") or {}, state.get("project_id"))
        return {"answer": text or "", "resolved_by": "file_ops"}

    def _plan(self, state: AgentState) -> dict[str, Any]:
        tools = self._tool_specs()
        if not tools:
            return {"tool_calls": [], "iterations": state.get("iterations", 0) + 1, "answer": ""}
        prepared = self.coordinator.prepare_turn(state["user_message"], state.get("project_id"))
        system_prompt = f"{prepared['system_prompt']}\n\n{TOOL_GUIDANCE}"
        context_block = str(prepared.get("context_block") or "").strip()
        if context_block:
            system_prompt = f"{system_prompt}\n\n{context_block}"
        messages = [ChatMessage(role="system", content=system_prompt), ChatMessage(role="user", content=prepared["user_prompt"])]
        for result in state.get("tool_results") or []:
            messages.append(ChatMessage(role="tool", content=str(result.get("message", ""))[:4000], tool_name=str(result.get("name", ""))))
        request = LLMRequest(messages=tuple(messages), tools=tools, task="chat")
        response = self._chat(request, stream=not state.get("tool_results"))
        calls = [{"name": call.name, "arguments": dict(call.arguments)} for call in response.tool_calls]
        update: dict[str, Any] = {"tool_calls": calls, "iterations": state.get("iterations", 0) + 1}
        if not calls:
            update["answer"] = self.coordinator.complete_turn(prepared, response.content.strip(), selected_tool=state.get("selected_tool"), tool_arguments=state.get("tool_arguments") or {}, tool_status=state.get("tool_status") or {"status": "skipped"})
            update["resolved_by"] = state.get("resolved_by") or "model"
        return update

    @staticmethod
    def _after_plan(state: AgentState) -> str:
        if state.get("tool_calls"):
            return "act"
        return "end" if state.get("answer") else "answer"

    def _act(self, state: AgentState) -> dict[str, Any]:
        results: list[dict[str, Any]] = list(state.get("tool_results") or [])
        answer_parts: list[str] = []
        capability: dict[str, Any] | None = None
        awaiting: dict[str, Any] | None = None
        selected: str | None = None
        arguments: dict[str, Any] = {}
        status: dict[str, Any] = {"status": "success"}
        needs_model = False
        for call in state.get("tool_calls") or []:
            name = str(call.get("name", ""))
            args = dict(call.get("arguments") or {})
            selected = selected or name
            arguments = arguments or args
            outcome = self._run_tool(name, args, state["user_message"])
            results.append({"name": name, **outcome})
            if outcome.get("awaiting"):
                awaiting = outcome["awaiting"]
                status = {"status": "pending_confirmation"}
                answer_parts.append(str(outcome.get("message", "")))
                break
            if outcome.get("capability"):
                capability = outcome["capability"]
                answer_parts.append(str(outcome.get("message", "")))
                continue
            if outcome.get("status") == "success":
                answer_parts.append(str(outcome.get("message", "")))
            else:
                status = {"status": outcome.get("status", "failed"), "error": outcome.get("error")}
                answer_parts.append(str(outcome.get("message", "")))
            needs_model = needs_model or bool(outcome.get("needs_model"))
        update: dict[str, Any] = {
            "tool_results": results,
            "selected_tool": selected,
            "tool_arguments": arguments,
            "tool_status": status,
            "capability": capability,
            "awaiting": awaiting,
            "resolved_by": "capability" if capability else "tools",
        }
        if awaiting is not None:
            update["answer"] = "\n".join(part for part in answer_parts if part)
            return update
        if needs_model and state.get("iterations", 0) < self.max_iterations:
            update["tool_calls"] = []
            return update
        update["answer"] = "\n".join(part for part in answer_parts if part)
        update["tool_calls"] = []
        return update

    def _after_act(self, state: AgentState) -> str:
        if state.get("awaiting"):
            return "end"
        if state.get("answer"):
            return "answer"
        return "plan"

    def _answer(self, state: AgentState) -> dict[str, Any]:
        if state.get("resolved_by") in {"tools", "capability"} and state.get("answer"):
            prepared = self.coordinator.prepare_turn(state["user_message"], state.get("project_id"), for_tools=True)
            text = self.coordinator.complete_turn(
                prepared,
                state["answer"],
                selected_tool=state.get("selected_tool"),
                tool_arguments=state.get("tool_arguments") or {},
                tool_status=state.get("tool_status") or {"status": "success"},
                capability=state.get("capability"),
            )
            return {"answer": text}
        prepared = self.coordinator.prepare_turn(state["user_message"], state.get("project_id"))
        request = LLMRequest.from_prompts(prepared["system_prompt"], prepared["user_prompt"], task="code" if state.get("kind") == "code" else "chat")
        if state.get("kind") == "code":
            request = LLMRequest.from_prompts(prepared["system_prompt"] + "\n\n" + self.coordinator.code_guidance(), prepared["user_prompt"], task="code")
        response = self._chat(request, stream=True)
        text = self.coordinator.complete_turn(prepared, response.content.strip(), selected_tool=None, tool_arguments={}, tool_status={"status": "skipped"})
        return {"answer": text, "resolved_by": "model"}

    def _tool_specs(self) -> tuple[ToolSpec, ...]:
        specs: list[ToolSpec] = []
        if self.tool_registry is not None:
            specs.extend(self.tool_registry.model_tools())
        known = {spec.name for spec in specs}
        for spec in capability_tool_specs(self.knowledge_router):
            if spec.name not in known:
                specs.append(spec)
        return tuple(specs)

    def _chat(self, request: LLMRequest, *, stream: bool) -> LLMResponse:
        client = self.coordinator.ollama_client
        on_delta = self._turn.get("on_delta")
        cancel_event = self._turn.get("cancel_event")
        streamer = getattr(client, "chat_stream", None)
        if stream and on_delta is not None and callable(streamer):
            parts: list[str] = []
            final: LLMResponse | None = None
            iterator = streamer(request)
            try:
                for item in iterator:
                    if isinstance(item, str):
                        if item:
                            parts.append(item)
                            on_delta(item)
                        if cancel_event is not None and cancel_event.is_set():
                            break
                    elif isinstance(item, LLMResponse):
                        final = item
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
            if final is not None and (final.tool_calls or final.content.strip()):
                return final
            text = "".join(parts).strip()
            if not text:
                raise OllamaClientError("Ollama response did not contain usable content")
            return LLMResponse(content=text)
        chat = getattr(client, "chat", None)
        if callable(chat):
            response = chat(request)
            if isinstance(response, LLMResponse):
                return response
        system_prompt = next((message.content for message in request.messages if message.role == "system"), "")
        user_prompt = next((message.content for message in reversed(request.messages) if message.role == "user"), "")
        text = client.generate(system_prompt, user_prompt, task=request.task)
        return LLMResponse(content=str(text or "").strip())

    def _run_tool(self, name: str, arguments: dict[str, Any], user_message: str) -> dict[str, Any]:
        executor = self.action_executor
        turn = getattr(self, "_turn", None) or {}
        if executor is not None and hasattr(executor, "cancel_event"):
            executor.cancel_event = turn.get("cancel_event")
        self._notify_tool({"phase": "start", "name": name, "arguments": dict(arguments)})
        started = time.perf_counter()
        result = self._dispatch(name, arguments, user_message)
        self._audit_tool(name, arguments, result)
        summary = str(result.get("message") or "").strip().splitlines()
        self._notify_tool({"phase": "end", "name": name, "status": result.get("status"), "error": result.get("error"), "ms": (time.perf_counter() - started) * 1000, "summary": summary[0] if summary else "", "target": str(result.get("resolved_target") or arguments.get("path") or arguments.get("range") or "")})
        return result

    def _notify_tool(self, event: dict[str, Any]) -> None:
        callback = self.on_tool_event
        if callback is None:
            return
        try:
            callback(event)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.debug("Tool progress callback failed: %s", error)

    def _dispatch(self, name: str, arguments: dict[str, Any], user_message: str) -> dict[str, Any]:
        definition = self.tool_registry.get(name) if self.tool_registry is not None else None
        enabled = definition is not None and self.tool_registry.is_enabled(name)
        if enabled and definition.kind == ToolKind.ACTION and self.action_executor is not None:
            return self._run_action(name, arguments, user_message)
        as_command = enabled and definition.kind == ToolKind.COMMAND
        if not as_command and self.knowledge_router is None:
            return {"status": "failed", "error": "unknown_tool", "message": f"I do not have a tool named {name}."}
        refusal = self._permission_refusal(name, definition if enabled else None, arguments)
        if refusal is not None:
            return refusal
        if as_command:
            return self._run_command(definition, arguments, user_message)
        return self._run_capability(name, arguments)

    def _permission_refusal(self, name: str, definition: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if self.permissions is None:
            return None
        request = PermissionRequest(
            tool=name,
            permission=getattr(definition, "permission", PermissionLevel.READ),
            action=name,
            source="agent",
            paths=paths_in(arguments),
            hosts=hosts_in(arguments),
            outbound=bool(getattr(definition, "outbound", True)),
        )
        decision = self.permissions.enforce(request)
        if decision.denied:
            return {"status": "failed", "error": "permission_denied", "message": f"Not allowed: {decision.reason}."}
        if decision.requires_confirmation and not interrupt({"tool": name, "arguments": arguments, "summary": decision.reason}):
            return {"status": "cancelled", "error": None, "message": f"{name} was not run."}
        return None

    def _audit_tool(self, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        if self.tool_auditor is None:
            return
        try:
            self.tool_auditor.record(name, arguments, result, source="agent")
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.warning("Tool audit failed for %s: %s", name, error)

    def _run_command(self, definition: Any, arguments: dict[str, Any], user_message: str) -> dict[str, Any]:
        handler = definition.handler
        if handler is None:
            return {"status": "failed", "error": "no_handler", "message": f"{definition.name} has no handler."}
        try:
            validated = definition.validate_arguments(arguments)
        except ToolArgumentError as error:
            return {"status": "failed", "error": "invalid_arguments", "message": f"I could not run {definition.name}: {error}"}
        handled = bool(handler(validated, {}))
        if handled and self.on_tool_success is not None:
            self.on_tool_success(user_message, definition.name, validated)
        return {"status": "success" if handled else "failed", "message": "" if handled else f"{definition.name} did not run."}

    def _run_action(self, name: str, arguments: dict[str, Any], user_message: str) -> dict[str, Any]:
        executor = self.action_executor
        has_pending = getattr(executor, "has_pending_confirmation", None)
        if callable(has_pending) and has_pending():
            message = executor.pending_description() or "An action is awaiting confirmation."
            result = None
        else:
            result = executor.execute(ActionRequest(action=name, arguments=arguments, source="agent", reason=f"Agent: {name}"))
            message = result.message
        if result is None or result.status == "pending_confirmation":
            preview = executor.pending_confirmation_preview()
            payload = {
                "tool": name,
                "arguments": arguments,
                "message": message,
                "summary": getattr(preview, "summary", None) or executor.pending_description() or message,
            }
            approved = interrupt(payload)
            if approved:
                confirmed = self.action_executor.confirm_pending()
                if confirmed.status == "success" and self.on_tool_success is not None:
                    self.on_tool_success(user_message, name, arguments)
                return {"status": confirmed.status, "error": confirmed.error, "message": confirmed.message, "results": to_json_list(confirmed.results)}
            cancelled = self.action_executor.cancel_pending()
            return {"status": "cancelled", "error": None, "message": cancelled.message}
        if result.status == "success" and self.on_tool_success is not None:
            self.on_tool_success(user_message, name, arguments)
        return {"status": result.status, "error": result.error, "message": result.message, "results": to_json_list(result.results)}

    def _run_capability(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        router = self.knowledge_router
        request_obj = router.build_capability_request(name, arguments)
        if request_obj is None:
            return {"status": "failed", "error": "invalid_request", "message": f"I need a valid {name} request before I can continue."}
        try:
            route_result = router.execute_capability_request(name, request_obj)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.warning("Capability %s failed: %s", name, error)
            return {"status": "failed", "error": "capability_failed", "message": f"I could not execute the {name} capability."}
        if route_result is None or route_result.response is None:
            notice = getattr(route_result, "fallback_notice", None) if route_result is not None else None
            return {"status": "partial", "error": "no_result", "message": notice or f"I could not retrieve a {name} result."}
        capability = {
            "provider": route_result.provider,
            "detail_type": getattr(route_result, "detail_type", "text"),
            "detail_title": getattr(route_result, "detail_title", None),
            "detail_content": getattr(route_result, "detail_content", None),
            "metadata": getattr(route_result, "metadata", None) or {},
        }
        return {"status": "success", "message": str(route_result.response).strip(), "capability": capability}
