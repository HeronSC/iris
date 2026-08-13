from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.application.contracts import ActionSuggestion, IrisMessage, IrisStatus, MessageRole
from core.application.service import IrisApplication
from core.assistant.coordinator import AssistantCoordinator
from core.assistant.prompting import PromptRequest, PromptType
from core.conversation.session import ConversationSession
from core.llm.ollama_client import OllamaClientError


class _FalseHandler:
    def handle(self, *_args, **_kwargs) -> bool:
        return False


class _ActionFalseHandler(_FalseHandler):
    def handle_natural_language(self, *_args, **_kwargs) -> bool:
        return False


class _SearchFalseHandler(_FalseHandler):
    def handle_natural_language(self, *_args, **_kwargs) -> bool:
        return False


class _SearchCaptureHandler(_FalseHandler):
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls = 0

    def handle_natural_language(self, *_args, **_kwargs) -> bool:
        self.calls = self.calls + 1
        return self.result


class _PendingFalse:
    def handle(self, *_args, **_kwargs) -> bool:
        return False


class _IntentFalse:
    def handle(self, *_args, **_kwargs) -> bool:
        return False


class _ActionExecutorStub:
    def has_pending_confirmation(self) -> bool:
        return False


class _CoordinatorStub:
    def __init__(self) -> None:
        self.respond_calls = 0

    def respond(self, *_args, **_kwargs) -> str:
        self.respond_calls = self.respond_calls + 1
        return "ok"

    def summarize_for_chat(self, **_kwargs) -> str:
        return "short conversational summary"

    def suggest_follow_up_actions(self, **_kwargs) -> list[ActionSuggestion]:
        return [
            ActionSuggestion(id="follow-up-1", label="More on habitat", payload={"command": "Tell me more about habitat"}),
            ActionSuggestion(id="follow-up-2", label="More on behavior", payload={"command": "Tell me more about behavior"}),
        ]


class _CoordinatorFactsStub:
    def __init__(self) -> None:
        self._last_general_knowledge_result = {
            "provider": "stocks",
            "detail_type": "markdown",
            "detail_title": "Stock Quote - NVDA",
            "detail_content": "legacy provider prose",
            "metadata": {
                "capability": "stocks",
                "facts": {
                    "ticker": "NVDA",
                    "price": 102.0,
                    "open": 100.0,
                    "change": 2.0,
                    "percent_change": 2.0,
                },
                "source": "stooq.com",
                "retrieved_at": "2026-08-03T20:30:00+00:00",
                "coverage": {"summary": "single_quote"},
                "warnings": [],
            },
        }

    def respond(self, *_args, **_kwargs) -> str:
        return "NVDA: $102.00 (+2.00, +2.00%)"

    def summarize_for_chat(self, **_kwargs) -> str:
        return "NVDA is at $102.00, +2.00 (+2.00%) for this session."

    def suggest_follow_up_actions(self, **_kwargs) -> list[ActionSuggestion]:
        return []


class _CoordinatorSessionManagerStub:
    def __init__(self, session: ConversationSession) -> None:
        self._session = session

    def get_active_session(self):
        return self._session

    def add_message(self, role: str, text: str, metadata=None) -> None:
        self._session.add_message(role, text, metadata=metadata)

    def save_active_session(self) -> None:
        return None

    def get_most_recent_session_id(self):
        return None

    def resume_session(self, _session_id) -> None:
        return None

    def start_session(self, title: str = "New Session") -> None:
        _ = title
        return None


class _CoordinatorRequestPipelineStub:
    def build_request(self, *_args, **_kwargs):
        return SimpleNamespace(
            intent="chat",
            requires_tool=False,
            response_allowed=True,
            target={},
            scope=None,
            filters={},
        )


class _CoordinatorFileIntentPipelineStub:
    def build_request(self, user_message: str, *_args, **_kwargs):
        lowered = str(user_message or "").strip().lower()
        if "file" in lowered and ("find" in lowered or "search" in lowered or "list" in lowered or "show" in lowered):
            return SimpleNamespace(
                intent="find_files",
                requires_tool=True,
                response_allowed=False,
                target={"query": "stub"},
                scope=None,
                filters={},
            )
        return SimpleNamespace(
            intent="respond",
            requires_tool=False,
            response_allowed=True,
            target={},
            scope=None,
            filters={},
        )


class _CoordinatorLLMStub:
    def generate(self, _system_prompt: str, _user_message: str) -> str:
        return "Monotremes are mammals that lay eggs; the main living examples are the platypus and echidnas."


class _SummaryLLMStub:
    def __init__(self, output: str, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.system_prompt: str | None = None
        self.user_prompt: str | None = None

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        if self.error is not None:
            raise self.error
        return self.output


class _CaptureLLMStub:
    def __init__(self, response: str = "ok") -> None:
        self.response = response
        self.system_prompt: str | None = None
        self.user_prompt: str | None = None

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.response


class _CoordinatorCancelBeforeSummaryStub:
    def __init__(self, cancel_event: threading.Event) -> None:
        self.cancel_event = cancel_event
        self.summary_calls = 0

    def respond(self, *_args, **_kwargs) -> str:
        self.cancel_event.set()
        return "ok"

    def summarize_for_chat(self, **_kwargs) -> str:
        self.summary_calls += 1
        return "should-not-be-used"


class _CoordinatorTraceLoggerStub:
    def log(self, *_args, **_kwargs) -> None:
        return None


class _CancelingIndexHandler:
    def handle(self, _text: str, state: dict[str, object]) -> bool:
        cancel_event = state.get("cancel_event")
        if isinstance(cancel_event, threading.Event):
            cancel_event.set()
        return True


class _PromptActionHandler(_ActionFalseHandler):
    def __init__(self, app: IrisApplication) -> None:
        self._app = app

    def handle(self, *_args, **_kwargs) -> bool:
        self._app._prompt(
            PromptRequest(
                prompt_id="confirm-demo",
                prompt_type=PromptType.YES_NO_CANCEL,
                text="Proceed with operation?",
                choices=("yes", "no", "cancel"),
            )
        )
        return True


class IrisApplicationPromptAndCancelTests(unittest.TestCase):
    def _build_app(self, prompt_provider=None) -> IrisApplication:
        app = IrisApplication(prompt_provider=prompt_provider)
        app.initialized = True
        app.state = {"assistant_name": "Iris"}
        app.action_executor = _ActionExecutorStub()
        app.pending_action_manager = _PendingFalse()
        app.save_handler = _FalseHandler()
        app.session_handler = _FalseHandler()
        app.proposal_handler = _FalseHandler()
        app.memory_handler = _FalseHandler()
        app.search_handler = _SearchFalseHandler()
        app.action_handler = _ActionFalseHandler()
        app.intent_router = _IntentFalse()
        app.project_handler = _FalseHandler()
        app.index_handler = _FalseHandler()
        app.coordinator = _CoordinatorStub()
        return app

    def test_process_message_returns_cancelled_when_handler_sets_cancel_event(self) -> None:
        app = self._build_app(prompt_provider=lambda _prompt: "")
        app.index_handler = _CancelingIndexHandler()

        response = app.process_message("/index scan", cancel_event=threading.Event())

        self.assertEqual(response.status, IrisStatus.CANCELLED)

    def test_prompt_interaction_messages_are_in_response(self) -> None:
        app = self._build_app(prompt_provider=lambda _prompt: "yes")
        app.action_handler = _PromptActionHandler(app)

        response = app.process_message("/launch test-app")

        self.assertEqual(response.status, IrisStatus.COMPLETE)
        rendered = [(message.role, message.text) for message in response.messages]
        self.assertIn((MessageRole.CONFIRMATION, "Proceed with operation?"), rendered)
        self.assertIn((MessageRole.USER, "yes"), rendered)

    def test_long_text_response_is_summarized_for_conversation(self) -> None:
        app = self._build_app()
        raw_text = (
            "Raccoons (*Procyon lotor*) are fascinating mammals native to North America, known for their distinctive black mask, fluffy tail, and dexterous paws. Here’s a quick overview:\n\n"
            "### **Physical Traits**\n"
            "- **Appearance**: They have a sleek, stocky body, a black mask around their eyes, and a bushy tail.\n"
            "- **Size**: Adults weigh 10-25 pounds and measure 25-40 inches in length."
        )

        conversation = app._build_conversation_content([IrisMessage(MessageRole.ASSISTANT, raw_text)], response_type="text")

        self.assertNotEqual(conversation.message, raw_text)
        self.assertLess(len(conversation.message), len(raw_text))
        self.assertNotIn("###", conversation.message)

    def test_conversation_summary_prefers_details_summary(self) -> None:
        app = self._build_app()
        messages = [IrisMessage(MessageRole.ASSISTANT, "full raw answer that should stay out of chat")]
        details = app._build_details_content(messages, status=IrisStatus.COMPLETE, response_type="text", requires_confirmation=False)
        app._conversation_override_message = "short summary"
        conversation = app._build_conversation_content_from_details(details, messages, response_type="text")

        self.assertEqual(conversation.message, "short summary")

    def test_process_message_uses_coordinator_summary_override(self) -> None:
        app = self._build_app()

        response = app.process_message("tell me about monotremes")

        self.assertEqual(response.status, IrisStatus.COMPLETE)
        self.assertEqual(response.conversation.message, "short conversational summary")
        self.assertTrue(isinstance(response.details.summary, str))
        self.assertIn("ok", response.details.summary)

    def test_process_message_marks_general_llm_details_as_markdown(self) -> None:
        app = self._build_app()

        response = app.process_message("tell me about monotremes")

        self.assertEqual(response.status, IrisStatus.COMPLETE)
        self.assertEqual(response.details.type, "markdown")
        self.assertEqual(len(response.details.actions), 2)

    def test_process_message_prefers_coordinator_for_file_intent_over_generic_search(self) -> None:
        app = self._build_app()
        coordinator = _CoordinatorStub()
        coordinator.request_pipeline = _CoordinatorFileIntentPipelineStub()
        app.coordinator = coordinator
        search_handler = _SearchCaptureHandler(result=True)
        app.search_handler = search_handler

        response = app.process_message("find files with navision in the name")

        self.assertEqual(response.status, IrisStatus.COMPLETE)
        self.assertEqual(coordinator.respond_calls, 1)
        self.assertEqual(search_handler.calls, 0)

    def test_process_message_keeps_non_file_routing_for_plain_queries(self) -> None:
        app = self._build_app()
        coordinator = _CoordinatorStub()
        app.coordinator = coordinator
        search_handler = _SearchCaptureHandler(result=True)
        app.search_handler = search_handler

        response = app.process_message("tell me about monotremes")

        self.assertEqual(response.status, IrisStatus.COMPLETE)
        self.assertEqual(coordinator.respond_calls, 0)
        self.assertEqual(search_handler.calls, 1)

    def test_process_message_renders_details_from_structured_facts(self) -> None:
        app = self._build_app()
        app.coordinator = _CoordinatorFactsStub()

        response = app.process_message("what is nvda trading at")

        self.assertEqual(response.status, IrisStatus.COMPLETE)
        self.assertIn("## Stock quote: NVDA", str(response.details.content))
        self.assertIn("Source: stooq.com", str(response.details.content))
        self.assertNotIn("legacy provider prose", str(response.details.content))

    def test_weather_next_week_details_are_range_aware(self) -> None:
        app = self._build_app()
        metadata = {
            "capability": "weather",
            "facts": {
                "location": "Anderson, SC",
                "range": "next_week",
                "headline": "Next week's forecast is not available through this source.",
                "condition": "Sunny",
                "current_temp_f": "87.8F",
                "rain_summary": "Rain is possible today (peak chance around 36%).",
                "details": ["- Currently available forecast window:", "  - Monday: Overcast", "  - Tuesday: Clear"],
            },
            "source": "wttr.in",
            "coverage": {"summary": "provider_limited_window"},
            "warnings": ["Requested next_week is outside provider forecast window."],
        }

        content = app._render_capability_details_from_facts(metadata)
        self.assertIn("## Next week forecast in Anderson, SC", content)
        self.assertIn("Summary: Next week's forecast is not available through this source.", content)
        self.assertIn("Forecast details:", content)
        self.assertNotIn("Current temperature: 87.8F", content)

    def test_capability_details_use_stable_section_ids_per_variant(self) -> None:
        app = self._build_app()
        messages = [IrisMessage(MessageRole.ASSISTANT, "weather result")]

        app._detail_type_override = "markdown"
        app._detail_content_override = "Current weather details"
        app._detail_metadata_override = {"capability": "weather", "weather_range": "current"}
        current_details = app._build_details_content(messages, status=IrisStatus.COMPLETE, response_type="markdown", requires_confirmation=False)

        app._detail_type_override = "markdown"
        app._detail_content_override = "Week forecast details"
        app._detail_metadata_override = {"capability": "weather", "weather_range": "week"}
        week_details = app._build_details_content(messages, status=IrisStatus.COMPLETE, response_type="markdown", requires_confirmation=False)

        self.assertEqual(current_details.section_id, "capability-weather-current")
        self.assertEqual(week_details.section_id, "capability-weather-week")

    def test_process_message_sets_topic_continue_on_follow_up(self) -> None:
        app = self._build_app()

        first = app.process_message("tell me about platypus")
        second = app.process_message("Tell me more about its venom")

        self.assertIsNotNone(first.topic)
        self.assertIsNotNone(second.topic)
        self.assertEqual(first.topic.relationship, "new_topic")
        self.assertEqual(second.topic.relationship, "continue")
        self.assertEqual(first.topic.id, second.topic.id)

    def test_process_message_sets_new_topic_for_new_subject(self) -> None:
        app = self._build_app()

        first = app.process_message("tell me about platypus")
        second = app.process_message("Now tell me about llamas")

        self.assertIsNotNone(first.topic)
        self.assertIsNotNone(second.topic)
        self.assertEqual(second.topic.relationship, "new_topic")
        self.assertNotEqual(first.topic.id, second.topic.id)
        self.assertEqual(second.details.metadata.get("render_operation"), "replace_workspace")

    def test_process_message_skips_summary_when_cancelled_before_summary(self) -> None:
        app = self._build_app()
        cancel_event = threading.Event()
        coordinator = _CoordinatorCancelBeforeSummaryStub(cancel_event)
        app.coordinator = coordinator

        response = app.process_message("tell me about monotremes", cancel_event=cancel_event)

        self.assertEqual(response.status, IrisStatus.CANCELLED)
        self.assertEqual(coordinator.summary_calls, 0)

    def test_coordinator_does_not_prefix_fallback_notice(self) -> None:
        with tempfile.TemporaryDirectory(prefix="iris-coordinator-test-") as temp_dir:
            temp_path = Path(temp_dir)
            config = {
                "assistant_name": "Iris",
                "memory_path": temp_path / "memory.json",
                "session_path": temp_path / "sessions",
                "proposal_path": temp_path / "proposals",
                "audit_path": temp_path / "audit",
                "llm_server": "http://localhost:11434",
                "model": "qwen3:8b",
                "conversation": {},
                "document_search": {"roots": [], "catalog_path": temp_path / "documents.db"},
                "applications": {},
                "web_shortcuts": {},
            }
            session = ConversationSession(max_messages=8)
            coordinator = AssistantCoordinator("Iris", object(), config, _CoordinatorLLMStub())
            coordinator.session = session
            coordinator.session_manager = _CoordinatorSessionManagerStub(session)
            coordinator.context_builder = SimpleNamespace(build_context=lambda *_args, **_kwargs: "")
            coordinator.request_pipeline = _CoordinatorRequestPipelineStub()
            coordinator.general_knowledge_router = SimpleNamespace(
                route=lambda _text: SimpleNamespace(
                    provider="lookup",
                    response=None,
                    fallback_notice="Lookup quick lookup could not answer that. Falling back to general knowledge...",
                )
            )
            coordinator.trace_logger = _CoordinatorTraceLoggerStub()

            response = coordinator.respond("what are the other monotremes")

            self.assertNotIn("Falling back to general knowledge", response)
            self.assertTrue(response.startswith("Monotremes are mammals"))

    def test_coordinator_summary_uses_dedicated_prompt_and_full_details(self) -> None:
        llm = _SummaryLLMStub(output="Besides the platypus, echidnas are the only other egg-laying mammals.")
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)
        detailed = (
            "Other mammals that lay eggs are monotremes.\n\n"
            "These include the platypus and echidnas, found mainly in Australia and New Guinea."
        )

        summary = coordinator.summarize_for_chat("what mammals lay eggs", detailed)

        self.assertIn("besides the platypus", summary.lower())
        self.assertIsNotNone(llm.system_prompt)
        self.assertIsNotNone(llm.user_prompt)
        self.assertIn("You rewrite assistant answers into concise conversational summaries.", llm.system_prompt or "")
        self.assertIn("User request:\nwhat mammals lay eggs", llm.user_prompt or "")
        self.assertIn("Australia and New Guinea", llm.user_prompt or "")

    def test_coordinator_summary_normalizes_markdown_without_dropping_content(self) -> None:
        llm = _SummaryLLMStub(
            output=(
                "### Key notes\n"
                "*Monotremes* are mammals that lay eggs.\n"
                "- They include platypus and echidnas."
            )
        )
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)

        summary = coordinator.summarize_for_chat("what mammals lay eggs", "raw")

        self.assertIn("Monotremes are mammals that lay eggs.", summary)
        self.assertIn("They include platypus and echidnas.", summary)
        self.assertNotIn("###", summary)
        self.assertNotIn("**", summary)

    def test_coordinator_summary_fallback_for_empty_output(self) -> None:
        llm = _SummaryLLMStub(output="   ")
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)

        summary = coordinator.summarize_for_chat(
            "what mammals lay eggs",
            "Other mammals that lay eggs include echidnas. They are monotremes.",
        )

        self.assertTrue(summary.endswith("."))
        self.assertNotEqual(summary, "")
        self.assertNotEqual(summary[-1], ":")

    def test_coordinator_summary_fallback_for_ollama_error(self) -> None:
        llm = _SummaryLLMStub(output="", error=OllamaClientError("timeout"))
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)

        summary = coordinator.summarize_for_chat("what mammals lay eggs", "Intro:\n- details")

        self.assertEqual(summary, "Intro: details")

    def test_coordinator_summary_uses_structured_stock_facts_without_llm(self) -> None:
        llm = _SummaryLLMStub(output="should-not-be-used")
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)
        coordinator._last_general_knowledge_result = {
            "provider": "stocks",
            "metadata": {
                "capability": "stocks",
                "facts": {
                    "ticker": "NVDA",
                    "price": 102.0,
                    "change": 2.0,
                    "percent_change": 2.0,
                },
            },
        }

        summary = coordinator.summarize_for_chat("what is nvda trading at", "NVDA: $102.00 (+2.00, +2.00%)")

        self.assertIn("NVDA is at $102.00", summary)
        self.assertIn("+2.00", summary)
        self.assertIsNone(llm.user_prompt)

    def test_coordinator_summary_uses_structured_news_facts_without_llm(self) -> None:
        llm = _SummaryLLMStub(output="should-not-be-used")
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)
        coordinator._last_general_knowledge_result = {
            "provider": "news",
            "metadata": {
                "capability": "news",
                "facts": {
                    "headlines": ["Alpha headline", "Beta headline"],
                },
            },
        }

        summary = coordinator.summarize_for_chat("news", "Top headlines:\n1. Alpha headline\n2. Beta headline")

        self.assertIn("Top headlines include", summary)
        self.assertIn("Alpha headline", summary)
        self.assertIsNone(llm.user_prompt)

    def test_coordinator_corrects_obvious_common_typo_for_plain_language(self) -> None:
        llm = _CaptureLLMStub(response="ok")
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)
        session = ConversationSession(max_messages=8)
        coordinator.session = session
        coordinator.session_manager = _CoordinatorSessionManagerStub(session)
        coordinator.context_builder = SimpleNamespace(build_context=lambda *_args, **_kwargs: "")
        coordinator.request_pipeline = _CoordinatorRequestPipelineStub()
        coordinator.general_knowledge_router = SimpleNamespace(route=lambda _text: None)
        coordinator.trace_logger = _CoordinatorTraceLoggerStub()

        coordinator.respond("Tell me about the playpus")

        self.assertEqual(llm.user_prompt, "Tell me about the platypus")

    def test_coordinator_does_not_autocorrect_paths_filenames_or_quoted_text(self) -> None:
        coordinator = AssistantCoordinator("Iris", object(), {}, _SummaryLLMStub(output="ok"))

        self.assertEqual(
            coordinator._prepare_user_message_for_llm("Open C:\\docs\\playpus.txt"),
            "Open C:\\docs\\playpus.txt",
        )
        self.assertEqual(
            coordinator._prepare_user_message_for_llm("Please run /search playpus"),
            "Please run /search playpus",
        )
        self.assertEqual(
            coordinator._prepare_user_message_for_llm('Tell me about "playpus"'),
            'Tell me about "playpus"',
        )

    def test_system_prompt_enforces_context_relevance_and_ambiguity_rules(self) -> None:
        coordinator = AssistantCoordinator("Iris", object(), {}, _SummaryLLMStub(output="ok"))
        coordinator.context_builder = SimpleNamespace(build_context=lambda *_args, **_kwargs: "- Location: Anderson")

        prompt = coordinator._build_system_prompt("Tell me about mercury")

        self.assertIn("Default to the most likely ordinary-language interpretation", prompt)
        self.assertIn("Ask for clarification only when multiple interpretations are genuinely plausible", prompt)
        self.assertIn("Do not introduce the user's location", prompt)
        self.assertIn("Do not autocorrect file paths, filenames, commands, identifiers, code, or quoted text", prompt)

    def test_coordinator_generates_structured_follow_up_actions(self) -> None:
        llm = _SummaryLLMStub(
            output='[{"label":"Effects on humans","prompt":"How does platypus venom affect humans?"},{"label":"Research","prompt":"How do researchers study platypus venom?"}]'
        )
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)

        actions = coordinator.suggest_follow_up_actions(
            user_message="Tell me more about platypus venom",
            detailed_response="Venom details",
            topic_title="Platypus",
        )

        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0].label, "Effects on humans")
        self.assertEqual(actions[0].payload.get("command"), "How does platypus venom affect humans?")

    def test_coordinator_follow_up_action_parsing_returns_empty_on_invalid_json(self) -> None:
        llm = _SummaryLLMStub(output="not json")
        coordinator = AssistantCoordinator("Iris", object(), {}, llm)

        actions = coordinator.suggest_follow_up_actions(
            user_message="Tell me more about platypus venom",
            detailed_response="Venom details",
            topic_title="Platypus",
        )

        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
