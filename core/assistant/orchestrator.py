from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.assistant.llm_client import LLMClient
from core.conversation.session import ConversationSession
from core.llm.ollama_client import OllamaClientError


class OrchestrationExecutionState(str, Enum):
    READY = "ready"
    EXECUTING = "executing"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class CapabilityCallPlanStep:
    capability_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class OrchestratorDecision:
    decision_type: str
    planned_steps: list[CapabilityCallPlanStep]
    clarification_question: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class OrchestrationOutcome:
    state: OrchestrationExecutionState
    route_result: Any | None = None
    chat_response: str | None = None
    selected_capability: str | None = None
    confidence: float | None = None

    def trace_payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "selected_capability": self.selected_capability,
            "confidence": self.confidence,
        }


class AssistantOrchestrator:
    def __init__(
        self,
        llm_client: LLMClient,
        router: Any,
        *,
        max_iterations: int = 2,
        default_weather_location_resolver: callable | None = None,
        orchestration_context_provider: callable | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.router = router
        self.max_iterations = max(1, int(max_iterations))
        self.default_weather_location_resolver = default_weather_location_resolver
        self.orchestration_context_provider = orchestration_context_provider

    def orchestrate(
        self,
        user_message: str,
        *,
        session: ConversationSession,
    ) -> OrchestrationOutcome:
        self._apply_defaults()

        decision = self._decide(user_message=user_message, session=session)
        if decision is None:
            return self._clarification_outcome()

        if decision.decision_type == "clarify":
            question = (decision.clarification_question or "").strip()
            if not question:
                return self._clarification_outcome()
            return OrchestrationOutcome(
                state=OrchestrationExecutionState.AWAITING_CLARIFICATION,
                chat_response=question,
                confidence=decision.confidence,
            )

        if decision.decision_type == "respond":
            return OrchestrationOutcome(
                state=OrchestrationExecutionState.READY,
                confidence=decision.confidence,
            )

        if decision.decision_type != "tool_plan" or not decision.planned_steps:
            return self._clarification_outcome()

        definitions = self._capability_definitions()
        if not definitions:
            return self._clarification_outcome()

        step = decision.planned_steps[0]
        step_name = step.capability_name
        if step_name not in definitions:
            return OrchestrationOutcome(
                state=OrchestrationExecutionState.AWAITING_CLARIFICATION,
                chat_response=f"I can help with this, but I do not have a capability named '{step_name}'.",
                confidence=decision.confidence,
            )

        request_obj = self._build_capability_request(step_name, step.arguments)
        if request_obj is None:
            return OrchestrationOutcome(
                state=OrchestrationExecutionState.AWAITING_CLARIFICATION,
                chat_response=f"I need a valid {step_name} request before I can continue.",
                selected_capability=step_name,
                confidence=decision.confidence,
            )

        execute_method = getattr(self.router, "execute_capability_request", None)
        if not callable(execute_method):
            return self._clarification_outcome()

        try:
            route_result = execute_method(step_name, request_obj)
        except Exception:
            return OrchestrationOutcome(
                state=OrchestrationExecutionState.FAILED,
                chat_response=f"I could not execute the {step_name} capability.",
                selected_capability=step_name,
                confidence=decision.confidence,
            )

        if route_result is None:
            return OrchestrationOutcome(
                state=OrchestrationExecutionState.PARTIALLY_COMPLETED,
                chat_response=f"I could not retrieve a {step_name} result.",
                selected_capability=step_name,
                confidence=decision.confidence,
            )

        return OrchestrationOutcome(
            state=OrchestrationExecutionState.COMPLETED,
            route_result=route_result,
            selected_capability=step_name,
            confidence=decision.confidence,
        )

    def _apply_defaults(self) -> None:
        set_weather_default_location = getattr(self.router, "set_weather_default_location", None)
        if not callable(set_weather_default_location):
            return
        if self.default_weather_location_resolver is None:
            return
        try:
            location = self.default_weather_location_resolver()
        except Exception:
            return
        set_weather_default_location(location)

    def _capability_definitions(self) -> dict[str, dict[str, Any]]:
        method = getattr(self.router, "capability_definitions", None)
        if not callable(method):
            return {}
        raw = method()
        if not isinstance(raw, list):
            return {}
        mapped: dict[str, dict[str, Any]] = {}
        for item in raw:
            name = str(getattr(item, "name", "")).strip()
            description = str(getattr(item, "description", "")).strip()
            request_schema = getattr(item, "request_schema", {})
            if not name:
                continue
            if not isinstance(request_schema, dict):
                request_schema = {}
            mapped[name] = {"description": description, "request_schema": request_schema}
        return mapped

    def _build_capability_request(self, capability_name: str, arguments: dict[str, Any]) -> Any | None:
        method = getattr(self.router, "build_capability_request", None)
        if not callable(method):
            return None
        if not isinstance(arguments, dict):
            return None
        return method(capability_name, arguments)

    def _decide(self, *, user_message: str, session: ConversationSession) -> OrchestratorDecision | None:
        capabilities = self._capability_definitions()
        if not capabilities:
            return OrchestratorDecision(decision_type="respond", planned_steps=[])

        tracked_context = self._build_tracked_context(session)

        history = session.get_messages()[-6:] if hasattr(session, "get_messages") else []
        history_lines: list[str] = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role", "")).strip()
            content = str(entry.get("content", "")).strip()
            if role and content:
                history_lines.append(f"- {role}: {content}")

        active_capability = str(session.metadata.get("active_capability", "")).strip() if hasattr(session, "metadata") else ""
        active_topic = str(session.metadata.get("active_topic", "")).strip() if hasattr(session, "metadata") else ""
        pending = session.pending_interaction if hasattr(session, "pending_interaction") else None
        known_defaults = tracked_context.get("known_defaults", {}) if isinstance(tracked_context.get("known_defaults", {}), dict) else {}
        known_entities = tracked_context.get("known_entities", {}) if isinstance(tracked_context.get("known_entities", {}), dict) else {}

        forced_weather_args = self._forced_weather_alert_arguments(
            user_message=user_message,
            active_capability=active_capability,
            known_defaults=known_defaults,
        )
        if forced_weather_args is not None and "weather" in capabilities:
            return OrchestratorDecision(
                decision_type="tool_plan",
                planned_steps=[CapabilityCallPlanStep(capability_name="weather", arguments=forced_weather_args)],
                confidence=1.0,
            )

        capability_lines: list[str] = []
        for name, payload in capabilities.items():
            description = str(payload.get("description", "")).strip()
            schema = payload.get("request_schema", {})
            capability_lines.append(f"- {name}: {description} | schema={json.dumps(schema, ensure_ascii=True)}")

        prompt = (
            "Available capabilities and request schemas:\n"
            f"{chr(10).join(capability_lines) if capability_lines else '- none'}\n\n"
            "Current context:\n"
            f"- active_topic: {active_topic or 'none'}\n"
            f"- active_capability: {active_capability or 'none'}\n"
            f"- pending_confirmation_or_selection: {json.dumps(pending, ensure_ascii=True) if isinstance(pending, dict) else 'none'}\n\n"
            "Known defaults and entities:\n"
            f"- known_defaults: {json.dumps(known_defaults, ensure_ascii=True) if known_defaults else '{}'}\n"
            f"- known_entities: {json.dumps(known_entities, ensure_ascii=True) if known_entities else '{}'}\n\n"
            "Recent conversation:\n"
            f"{chr(10).join(history_lines) if history_lines else '- none'}\n\n"
            "User message:\n"
            f"{user_message}\n\n"
            "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps.\n"
            "decision must be one of: respond, clarify, tool.\n"
            "For decision=tool, provide capability and arguments, or provide steps as a non-empty array of objects {capability, arguments}.\n"
            "Do not answer the user's question directly."
        )

        try:
            payload = self.llm_client.generate(self._decision_system_prompt(), prompt)
        except (OllamaClientError, TimeoutError, OSError, ValueError):
            return None

        parsed = self._parse_json_object(payload)
        if not isinstance(parsed, dict):
            return None

        decision_type = str(parsed.get("decision", "")).strip().lower()
        confidence = self._parse_confidence(parsed.get("confidence"))

        if decision_type == "respond":
            return OrchestratorDecision(decision_type="respond", planned_steps=[], confidence=confidence)
        if decision_type == "clarify":
            question = str(parsed.get("question", "")).strip()
            return OrchestratorDecision(
                decision_type="clarify",
                planned_steps=[],
                clarification_question=question,
                confidence=confidence,
            )
        if decision_type != "tool":
            return None

        steps_raw = parsed.get("steps")
        steps: list[CapabilityCallPlanStep] = []
        if isinstance(steps_raw, list):
            for item in steps_raw[: self.max_iterations]:
                if not isinstance(item, dict):
                    continue
                capability_name = str(item.get("capability", "")).strip()
                arguments = item.get("arguments")
                if not capability_name or not isinstance(arguments, dict):
                    continue
                steps.append(CapabilityCallPlanStep(capability_name=capability_name, arguments=arguments))

        if not steps:
            capability_name = str(parsed.get("capability", "")).strip()
            arguments = parsed.get("arguments")
            if capability_name and isinstance(arguments, dict):
                steps = [CapabilityCallPlanStep(capability_name=capability_name, arguments=arguments)]

        if not steps:
            return None

        return OrchestratorDecision(decision_type="tool_plan", planned_steps=steps, confidence=confidence)

    def _forced_weather_alert_arguments(
        self,
        *,
        user_message: str,
        active_capability: str,
        known_defaults: dict[str, Any],
    ) -> dict[str, Any] | None:
        lowered = (user_message or "").strip().lower()
        if not lowered:
            return None

        alert_tokens = ("alert", "alerts", "warning", "warnings", "watch", "advisory", "update", "updates")
        weather_tokens = ("weather", "forecast", "outside", "rain", "storm", "temperature")
        has_alert_signal = any(token in lowered for token in alert_tokens)
        has_weather_signal = any(token in lowered for token in weather_tokens)

        if not has_alert_signal:
            return None
        if not has_weather_signal and active_capability != "weather":
            return None

        range_name = "current"
        start = "today"
        granularity = "current"
        if any(token in lowered for token in ("today", "tonight", "this afternoon", "this evening", "later today")):
            range_name = "today"
            start = "today"
            granularity = "hourly"
        elif "tomorrow" in lowered:
            range_name = "tomorrow"
            start = "tomorrow"
            granularity = "daily"

        location = self._extract_location_hint(user_message)
        if not location:
            default_location = str(known_defaults.get("weather_location", "")).strip() if isinstance(known_defaults, dict) else ""
            location = default_location

        args: dict[str, Any] = {
            "range_name": range_name,
            "start": start,
            "granularity": granularity,
            "focus": "rain",
        }
        if location:
            args["location"] = location
        return args

    def _extract_location_hint(self, user_message: str) -> str:
        text = (user_message or "").strip()
        if not text:
            return ""
        match = re.search(
            r"\b(?:in|for|at)\s+([A-Za-z][A-Za-z\s,-]{1,60}?)(?:\s+(?:today|tonight|tomorrow|this\s+afternoon|this\s+evening|now))?(?:[?.!]|$)",
            text,
            flags=re.IGNORECASE,
        )
        if match is None:
            return ""
        return match.group(1).strip(" .,")

    def _decision_system_prompt(self) -> str:
        return (
            "You are an orchestration planner for an assistant.\n"
            "Select whether the assistant should respond directly, ask a clarification, or call a capability with structured arguments.\n"
            "Do not use confidence as the primary decision rule; clarify when required arguments are missing, permissions are needed, or ambiguity is material.\n"
            "When weather is requested and known_defaults contains weather_location, use it if the user did not specify location.\n"
            "When weather is requested and no range is specified, default to current conditions.\n"
            "Return only JSON."
        )

    def _build_tracked_context(self, session: ConversationSession) -> dict[str, Any]:
        provider = self.orchestration_context_provider
        if not callable(provider):
            return {}
        try:
            payload = provider(session)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def _clarification_outcome(self) -> OrchestrationOutcome:
        return OrchestrationOutcome(
            state=OrchestrationExecutionState.AWAITING_CLARIFICATION,
            chat_response="I could not determine the right capability for that request. Please rephrase with the specific task you want me to perform.",
        )

    def _parse_json_object(self, text: str) -> dict[str, Any] | None:
        payload = (text or "").strip()
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
        return None

    def _parse_confidence(self, value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed < 0.0:
            return 0.0
        if parsed > 1.0:
            return 1.0
        return parsed