import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.assistant.coordinator import AssistantCoordinator
from core.conversation.request_pipeline import RequestPipeline, RequestPipelineResult
from core.conversation.session import ConversationSession
from core.conversation.session_manager import SessionManager


class MemoryStoreStub:
    def get_profile(self) -> dict[str, object]:
        return {"profile": {}}

    def get_active_preferences(self) -> list[dict[str, object]]:
        return []

    def get_project(self, project_id: str) -> dict[str, object] | None:
        return None

    def get_knowledge_areas(self) -> list[dict[str, object]]:
        return []


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return "summary-ready"


class WeatherIntentLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." in user_prompt:
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"week","start":"today","granularity":"daily"},"confidence":0.91}'
        return "summary-ready"


class StockIntentLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." in user_prompt:
            return '{"decision":"tool","capability":"stocks","arguments":{"ticker":"NVDA"},"confidence":0.87}'
        return "summary-ready"


class RespondIntentLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." in user_prompt:
            return '{"decision":"respond","confidence":0.82}'
        return "summary-ready"


class CrossTopicIntentLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." not in user_prompt:
            return "summary-ready"
        if "What meetings do I have tomorrow?" in user_prompt:
            return '{"decision":"tool","capability":"calendar","arguments":{"day":"tomorrow"},"confidence":0.95}'
        if "What about the afternoon?" in user_prompt:
            return '{"decision":"tool","capability":"calendar","arguments":{"day":"tomorrow","time_block":"afternoon"},"confidence":0.93}'
        return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"tomorrow","granularity":"daily"},"confidence":0.94}'


class PhaseAcceptanceIntentLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." not in user_prompt:
            return "summary-ready"
        current_message = user_prompt
        marker = "User message:\n"
        if marker in user_prompt:
            current_message = user_prompt.split(marker, 1)[1]
            current_message = current_message.split("\n\nReturn JSON only", 1)[0].strip()

        if current_message == "What is it like outside?":
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"current","granularity":"current"},"confidence":0.95}'
        if current_message == "What about tomorrow?":
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"tomorrow","granularity":"daily"},"confidence":0.94}'
        if current_message == "How does the weekend look?":
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"weekend","granularity":"daily"},"confidence":0.94}'
        if current_message == "Would Saturday be a good day to mow?":
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"saturday","granularity":"daily","focus":"mowing"},"confidence":0.93}'
        if current_message == "Find the spreadsheet I made about E*TRADE last month.":
            return '{"decision":"tool","capability":"files","arguments":{"action":"search","query":"E*TRADE spreadsheet last month","file_type":"excel"},"confidence":0.92}'
        if current_message == "Open the second one.":
            return '{"decision":"tool","capability":"files","arguments":{"action":"open","selection":2},"confidence":0.91}'
        if current_message == "No, remove that one from the results.":
            return '{"decision":"tool","capability":"files","arguments":{"action":"exclude","selection":2},"confidence":0.91}'
        if current_message == "What was the other Excel file?":
            return '{"decision":"tool","capability":"files","arguments":{"action":"recall_other","file_type":"excel"},"confidence":0.90}'
        if current_message == "What do I have tomorrow?":
            return '{"decision":"tool","capability":"calendar","arguments":{"day":"tomorrow"},"confidence":0.95}'
        if current_message == "Move the later one to Friday.":
            return '{"decision":"tool","capability":"calendar","arguments":{"action":"move","selection":"later","target_day":"friday"},"confidence":0.93}'
        if current_message == "What meetings do I have tomorrow?":
            return '{"decision":"tool","capability":"calendar","arguments":{"day":"tomorrow"},"confidence":0.95}'
        if current_message == "What about the afternoon?":
            return '{"decision":"tool","capability":"calendar","arguments":{"day":"tomorrow","time_block":"afternoon"},"confidence":0.94}'
        if current_message == "What is tomorrow's forecast?":
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"tomorrow","granularity":"daily"},"confidence":0.95}'
        return '{"decision":"respond","confidence":0.60}'


class NeedsDefaultsWeatherLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." not in user_prompt:
            return "summary-ready"
        if "known_defaults" in user_prompt and "Anderson, SC" in user_prompt and "what is the weather" in user_prompt.lower():
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"current","granularity":"current"},"confidence":0.92}'
        return '{"decision":"clarify","question":"Could you specify location and time period?","confidence":0.40}'


class WeeklyWeatherIntentLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." in user_prompt:
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"week","granularity":"daily"},"confidence":0.93}'
        return "summary-ready"


class NextWeekWeatherIntentLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." in user_prompt:
            return '{"decision":"tool","capability":"weather","arguments":{"location":"Anderson, SC","range_name":"next_week","granularity":"daily"},"confidence":0.94}'
        return "summary-ready"


class WeatherRouterStub:
    def __init__(self) -> None:
        self.default_location: str | None = None
        self.active_provider: str | None = None
        self.route_provider_calls: list[tuple[str, str]] = []
        self.execute_request_calls: list[tuple[str, object]] = []

    def set_weather_default_location(self, location: str | None) -> None:
        self.default_location = location

    def set_active_provider(self, provider_name: str | None) -> None:
        self.active_provider = provider_name

    def route_provider(self, provider_name: str, text: str):
        self.route_provider_calls.append((provider_name, text))
        response_text = "Current weather in Anderson, SC: Sunny, 82.0F." if provider_name == "weather" else "NVDA: $123.45 (+1.20, +0.98%)"
        title = "Current Weather - Anderson, Sc" if provider_name == "weather" else "Stock Quote - NVDA"
        detail = "weather details" if provider_name == "weather" else "stock details"
        return type(
            "Result",
            (),
            {
                "provider": provider_name,
                "response": response_text,
                "fallback_notice": None,
                "detail_type": "markdown",
                "detail_title": title,
                "detail_content": detail,
                "metadata": {"capability": provider_name},
            },
        )()

    def capability_definitions(self):
        return [
            type(
                "CapabilityDefinition",
                (),
                {
                    "name": "weather",
                    "description": "Weather capability",
                    "request_schema": {"location": "string", "range_name": "string", "start": "string", "granularity": "string", "period": "string|null", "focus": "string|null"},
                },
            )(),
            type(
                "CapabilityDefinition",
                (),
                {
                    "name": "stocks",
                    "description": "Stock capability",
                    "request_schema": {"ticker": "string"},
                },
            )(),
        ]

    def build_capability_request(self, provider_name: str, payload: dict[str, object]):
        if provider_name in {"weather", "stocks"}:
            return payload
        return None

    def execute_capability_request(self, provider_name: str, request_obj: object):
        self.execute_request_calls.append((provider_name, request_obj))
        return self.route_provider(provider_name, "typed")

    def route(self, text: str):
        _ = text
        return None


class CrossTopicRouterStub(WeatherRouterStub):
    def route_provider(self, provider_name: str, text: str):
        self.route_provider_calls.append((provider_name, text))
        if provider_name == "calendar":
            return type(
                "Result",
                (),
                {
                    "provider": "calendar",
                    "response": "You have two meetings tomorrow afternoon.",
                    "fallback_notice": None,
                    "detail_type": "markdown",
                    "detail_title": "Calendar - Tomorrow",
                    "detail_content": "Calendar details",
                    "metadata": {"capability": "calendar"},
                },
            )()
        return super().route_provider(provider_name, text)

    def capability_definitions(self):
        definitions = super().capability_definitions()
        definitions.append(
            type(
                "CapabilityDefinition",
                (),
                {
                    "name": "calendar",
                    "description": "Calendar capability",
                    "request_schema": {"day": "string", "time_block": "string|null"},
                },
            )()
        )
        return definitions

    def build_capability_request(self, provider_name: str, payload: dict[str, object]):
        if provider_name == "calendar":
            return payload
        return super().build_capability_request(provider_name, payload)


class PhaseAcceptanceRouterStub(CrossTopicRouterStub):
    def route_provider(self, provider_name: str, text: str):
        self.route_provider_calls.append((provider_name, text))
        return super().route_provider(provider_name, text)

    def capability_definitions(self):
        definitions = super().capability_definitions()
        definitions.append(
            type(
                "CapabilityDefinition",
                (),
                {
                    "name": "files",
                    "description": "File search and selection capability",
                    "request_schema": {"action": "string", "query": "string|null", "selection": "number|string|null", "file_type": "string|null"},
                },
            )()
        )
        return definitions

    def build_capability_request(self, provider_name: str, payload: dict[str, object]):
        if provider_name == "files":
            return payload
        return super().build_capability_request(provider_name, payload)

    def execute_capability_request(self, provider_name: str, request_obj: object):
        self.execute_request_calls.append((provider_name, request_obj))
        payload = request_obj if isinstance(request_obj, dict) else {}
        if provider_name == "weather":
            range_name = str(payload.get("range_name", "")).lower()
            if range_name == "current":
                response = "Current weather in Anderson, SC: Sunny, 82.0F."
            elif range_name == "tomorrow":
                response = "Tomorrow in Anderson, SC: High 84F, low 68F, light breeze."
            elif range_name == "weekend":
                response = "Weekend in Anderson, SC: Saturday dry and warm, Sunday scattered showers."
            else:
                response = "Saturday in Anderson, SC looks dry; mowing conditions are good by late morning."
            return type(
                "Result",
                (),
                {
                    "provider": "weather",
                    "response": response,
                    "fallback_notice": None,
                    "detail_type": "markdown",
                    "detail_title": "Weather",
                    "detail_content": "Weather details",
                    "metadata": {"capability": "weather"},
                },
            )()
        if provider_name == "files":
            action = str(payload.get("action", "")).lower()
            if action == "search":
                response = "Found matching files:\n1. ETRADE_Portfolio.xlsx\n2. ETRADE_Trades.xlsx"
            elif action == "open":
                response = "Opened ETRADE_Trades.xlsx"
            elif action == "exclude":
                response = "Removed ETRADE_Trades.xlsx from results"
            else:
                response = "The other Excel file was ETRADE_Portfolio.xlsx"
            return type(
                "Result",
                (),
                {
                    "provider": "files",
                    "response": response,
                    "fallback_notice": None,
                    "detail_type": "markdown",
                    "detail_title": "Files",
                    "detail_content": "Files details",
                    "metadata": {"capability": "files"},
                },
            )()
        if provider_name == "calendar":
            action = str(payload.get("action", "")).lower()
            time_block = str(payload.get("time_block", "")).lower()
            if action == "move":
                response = "Moved the later meeting to Friday."
            elif time_block == "afternoon":
                response = "Tomorrow afternoon you have one meeting at 3:00 PM."
            else:
                response = "Tomorrow you have two meetings."
            return type(
                "Result",
                (),
                {
                    "provider": "calendar",
                    "response": response,
                    "fallback_notice": None,
                    "detail_type": "markdown",
                    "detail_title": "Calendar",
                    "detail_content": "Calendar details",
                    "metadata": {"capability": "calendar"},
                },
            )()
        return super().execute_capability_request(provider_name, request_obj)


class PassThroughRespondPipeline:
    def build_request(self, user_message: str, state: dict[str, object] | None = None) -> RequestPipelineResult:
        _ = user_message
        _ = state
        return RequestPipelineResult(intent="respond", requires_tool=False, response_allowed=True)


class CountingContextBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def build_context(self, *args: object, **kwargs: object) -> str:
        self.calls = self.calls + 1
        return "context"


class InMemorySessionRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, ConversationSession] = {}
        self._counter = 0

    def create_session(self, title: str | None = None, project_id: str | None = None) -> ConversationSession:
        self._counter = self._counter + 1
        session = ConversationSession(max_messages=4)
        session.id = str(self._counter)
        session.title = title or "New Session"
        session.project_id = project_id
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> ConversationSession | None:
        return self.sessions.get(session_id)

    def save_session(self, session: ConversationSession) -> None:
        if session.id is None:
            session.id = str(self._counter + 1)
        self.sessions[session.id] = session

    def list_sessions(self, limit: int | None = None) -> list[dict[str, object]]:
        items = []
        for session_id, session in self.sessions.items():
            items.append({"id": session_id, "title": session.title, "project_id": session.project_id})
        if limit is not None:
            return items[:limit]
        return items


class PhaseFourPointEightSliceTwoTests(unittest.TestCase):
    def test_request_pipeline_handles_numeric_selection(self) -> None:
        session = ConversationSession(max_messages=4)
        session.pending_interaction = {"type": "file_selection", "results": [{"position": 1, "path": "E:/AI/notes.md"}]}
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("1", state={"session": session})

        self.assertEqual(request.intent, "select_pending_result")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["selection"], 1)

    def test_request_pipeline_resolves_ai_root_requests(self) -> None:
        pipeline = RequestPipeline(config={})
        request = pipeline.build_request("How many .md files are in the AI folder?", state={})

        self.assertEqual(request.intent, "count_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["path"], "E:/AI")
        self.assertEqual(request.scope["root_alias"], "AI")

    def test_coordinator_counts_files_with_configured_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "AI"
            root.mkdir(parents=True, exist_ok=True)
            (root / "one.md").write_text("one", encoding="utf-8")
            (root / "nested").mkdir(exist_ok=True)
            (root / "nested" / "two.md").write_text("two", encoding="utf-8")
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                    "roots": [{"name": "AI", "path": str(root)}],
                },
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            response = coordinator.respond("How many .md files are in the AI folder?")

            self.assertIn("2", response)
            self.assertEqual(fake_llm.calls, [])

    def test_coordinator_resolves_pending_selection_without_llm_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_llm = FakeLLM()
            session = ConversationSession(max_messages=4)
            session.pending_interaction = {
                "type": "file_selection",
                "results": [{"position": 1, "path": str(Path(tmpdir) / "alpha.md")}],
            }
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                session=session,
            )

            response = coordinator.respond("1")

            self.assertIn("alpha.md", response)
            self.assertEqual(fake_llm.calls, [])

    def test_coordinator_help_returns_registered_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            response = coordinator.respond("/help")

            self.assertIn("/help", response)
            self.assertIn("/index", response)
            self.assertEqual(fake_llm.calls, [])

    def test_coordinator_writes_request_trace_for_tool_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "notes.md"
            target.write_text("hello from the file", encoding="utf-8")
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            coordinator.respond(f"summarize this file {target}")

            trace_path = Path(tmpdir) / "request_trace.jsonl"
            self.assertTrue(trace_path.exists())
            payload = trace_path.read_text(encoding="utf-8")
            self.assertIn("read_file", payload)
            self.assertIn("tool_result", payload)

    def test_request_pipeline_detects_filename_lookup(self) -> None:
        pipeline = RequestPipeline(config={})
        request = pipeline.build_request("Find phase 4.5.md and summarize it.", state={})

        self.assertEqual(request.intent, "find_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["query"], "phase 4.5.md")

    def test_request_pipeline_detects_indexed_filename_lookup(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("can you look at indexed files for any file with bedroom or spare in the name?", state={})

        self.assertEqual(request.intent, "find_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["query"], "bedroom or spare")

    def test_request_pipeline_detects_list_files_with_name_clause(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("can you list any files with Ext in the name?", state={})

        self.assertEqual(request.intent, "find_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["query"], "Ext")

    def test_request_pipeline_detects_show_files_named_clause(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("show files named roadmap", state={})

        self.assertEqual(request.intent, "find_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["query"], "roadmap")

    def test_request_pipeline_detects_display_files_named_clause(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("display any files called release-notes", state={})

        self.assertEqual(request.intent, "find_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["query"], "release-notes")

    def test_request_pipeline_detects_files_contain_name_clause(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("list any files that contain EFT in the name", state={})

        self.assertEqual(request.intent, "find_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["query"], "EFT")

    def test_request_pipeline_detects_files_with_filler_words(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("show me all the files with anything like EXT in the name", state={})

        self.assertEqual(request.intent, "find_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["query"], "EXT")

    def test_request_pipeline_detects_first_numbered_files_variant(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("list the first 25 files containing roadmap in the name", state={})

        self.assertEqual(request.intent, "find_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.target["query"], "roadmap")

    def test_request_pipeline_detects_count_by_number_of_phrase(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("what is the number of .md files in the AI folder?", state={})

        self.assertEqual(request.intent, "count_files")
        self.assertTrue(request.requires_tool)
        self.assertEqual(request.filters["extension"], ".md")

    def test_request_pipeline_detects_explicit_weather_request(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("what's it like outside today?", state={})

        self.assertEqual(request.intent, "respond")
        self.assertFalse(request.requires_tool)

    def test_request_pipeline_detects_weather_follow_up_from_active_capability(self) -> None:
        session = ConversationSession(max_messages=4)
        session.metadata["active_capability"] = "weather"
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("how about the rest of the week?", state={"session": session})

        self.assertEqual(request.intent, "respond")
        self.assertFalse(request.requires_tool)

    def test_orchestrator_uses_tracked_default_location_from_memory_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            class MemoryStoreWithLocation(MemoryStoreStub):
                def get_profile(self) -> dict[str, object]:
                    return {
                        "profile": {
                            "location": {
                                "city": "Anderson",
                                "region": "SC",
                            }
                        }
                    }

            llm = NeedsDefaultsWeatherLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreWithLocation(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=llm,
                session=ConversationSession(max_messages=4),
            )
            router_stub = WeatherRouterStub()
            coordinator.general_knowledge_router = router_stub

            response = coordinator.respond("what is the weather")

            self.assertIn("Current weather in Anderson, SC", response)
            self.assertEqual(router_stub.execute_request_calls, [("weather", {"location": "Anderson, SC", "range_name": "current", "granularity": "current"})])
            decision_prompts = [entry[1] for entry in llm.calls if "Return JSON only as an object with keys decision, capability, arguments, question, confidence, and steps." in entry[1]]
            self.assertTrue(decision_prompts)
            self.assertIn('"weather_location": "Anderson, SC"', decision_prompts[0])

    def test_orchestrator_routes_weather_alert_update_queries_without_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = NeedsDefaultsWeatherLLM()
            session = ConversationSession(max_messages=4)
            session.metadata["active_capability"] = "weather"
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=llm,
                session=session,
            )
            router_stub = WeatherRouterStub()
            coordinator.general_knowledge_router = router_stub

            response = coordinator.respond("Are there any weather alerts or updates for Anderson today?")

            self.assertIn("Current weather", response)
            self.assertEqual(len(router_stub.execute_request_calls), 1)
            provider_name, payload = router_stub.execute_request_calls[0]
            self.assertEqual(provider_name, "weather")
            self.assertEqual(payload.get("range_name"), "today")
            self.assertEqual(payload.get("granularity"), "hourly")
            self.assertEqual(payload.get("focus"), "rain")
            self.assertEqual(session.metadata.get("active_capability"), "weather")

    def test_weekly_weather_response_is_not_overwritten_by_current_fact_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = WeeklyWeatherIntentLLM()
            session = ConversationSession(max_messages=4)
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=llm,
                session=session,
            )

            class WeeklyFactsRouterStub(WeatherRouterStub):
                def route_provider(self, provider_name: str, text: str):
                    self.route_provider_calls.append((provider_name, text))
                    return type(
                        "Result",
                        (),
                        {
                            "provider": "weather",
                            "response": "For Anderson, SC, Monday: Sunny. Tuesday: Rain likely. Wednesday: Overcast.",
                            "fallback_notice": None,
                            "detail_type": "markdown",
                            "detail_title": "Weekly Outlook - Anderson, SC",
                            "detail_content": "weekly details",
                            "metadata": {
                                "capability": "weather",
                                "facts": {
                                    "location": "Anderson, SC",
                                    "range": "week",
                                    "granularity": "daily",
                                    "condition": "Sunny",
                                    "current_temp_f": "87.8F",
                                    "rain_summary": "Rain is possible today (peak chance around 36%).",
                                },
                            },
                        },
                    )()

            router_stub = WeeklyFactsRouterStub()
            coordinator.general_knowledge_router = router_stub

            response = coordinator.respond("how about the rest of the week?")

            self.assertIn("Monday: Sunny", response)
            self.assertNotIn("Current weather in Anderson, SC", response)

    def test_next_week_weather_response_prefers_outcome_summary_over_coverage_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = NextWeekWeatherIntentLLM()
            session = ConversationSession(max_messages=4)
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=llm,
                session=session,
            )

            class NextWeekFactsRouterStub(WeatherRouterStub):
                def route_provider(self, provider_name: str, text: str):
                    self.route_provider_calls.append((provider_name, text))
                    return type(
                        "Result",
                        (),
                        {
                            "provider": "weather",
                            "response": "Next week's forecast for Anderson, SC is not available through this source. Provider coverage is limited to the next 3 day(s).",
                            "fallback_notice": None,
                            "detail_type": "markdown",
                            "detail_title": "Next Week Forecast - Anderson, SC",
                            "detail_content": "next week details",
                            "metadata": {
                                "capability": "weather",
                                "facts": {
                                    "location": "Anderson, SC",
                                    "range": "next_week",
                                    "granularity": "daily",
                                    "headline": "Next week's forecast for Anderson, SC is not available through this source.",
                                    "details": [
                                        "- Currently available forecast window:",
                                        "  - Monday: Overcast; 68.0F to 91.4F; Rain is possible monday (peak chance around 36%).",
                                        "  - Tuesday: Clear; 71.6F to 89.6F; Rain is likely tuesday (peak chance around 77%).",
                                        "  - Wednesday: Mist; 68.0F to 86.0F; Rain is likely wednesday (peak chance around 83%).",
                                        "Coverage note: Provider coverage is limited to the next 3 day(s).",
                                    ],
                                },
                            },
                        },
                    )()

            router_stub = NextWeekFactsRouterStub()
            coordinator.general_knowledge_router = router_stub

            response = coordinator.respond("how about next week?")

            self.assertIn("Available outlook:", response)
            self.assertIn("Monday: Overcast", response)
            self.assertNotIn("Provider coverage is limited to the next 3 day(s).", response)

    def test_open_path_returns_error_when_open_command_crashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "alpha.md"
            target.write_text("alpha body", encoding="utf-8")
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                    "roots": [{"name": "AI", "path": str(tmpdir)}],
                },
                ollama_client=FakeLLM(),
                session=ConversationSession(max_messages=4),
            )

            with patch("core.assistant.coordinator._platform_start_file", side_effect=RuntimeError("boom")):
                response = coordinator._open_path(target)

            self.assertIn("could not be opened", response.lower())

    def test_selection_executes_requested_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "alpha.md"
            target.write_text("alpha body", encoding="utf-8")
            fake_llm = FakeLLM()
            session = ConversationSession(max_messages=4)
            session.pending_interaction = {
                "type": "file_selection",
                "requested_action": "summarize_file",
                "results": [{"position": 1, "path": str(target)}],
            }
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                session=session,
            )

            response = coordinator.respond("1")

            self.assertEqual(response, "No approved file roots are configured.")
            self.assertFalse(fake_llm.calls)

    def test_search_results_are_numbered_and_preserve_requested_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "AI"
            root.mkdir(parents=True, exist_ok=True)
            (root / "phase 5.md").write_text("phase 5", encoding="utf-8")
            (root / "phase 6.md").write_text("phase 6", encoding="utf-8")
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                    "roots": [{"name": "AI", "path": str(root)}],
                },
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            response = coordinator.respond("Find phase and open it")

            self.assertIn("1.", response)
            self.assertIn("2.", response)
            self.assertEqual(fake_llm.calls, [])
            self.assertEqual(coordinator.session.pending_interaction["requested_action"], "open_path")

    def test_plain_filename_lookup_defaults_to_selection_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "AI"
            root.mkdir(parents=True, exist_ok=True)
            (root / "phase 5.md").write_text("phase 5", encoding="utf-8")
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                    "roots": [{"name": "AI", "path": str(root)}],
                },
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            coordinator.respond("Find phase 5.md")

            self.assertEqual(coordinator.session.pending_interaction["requested_action"], "select_file")

    def test_count_reports_nonexistent_root_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                },
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            response = coordinator.respond("Count .md files in E:/DoesNotExist")

            self.assertIn("no approved roots are configured", response.lower())
            self.assertEqual(fake_llm.calls, [])

    def test_invalid_selection_reports_valid_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "alpha.md"
            target.write_text("alpha body", encoding="utf-8")
            fake_llm = FakeLLM()
            session = ConversationSession(max_messages=4)
            session.pending_interaction = {
                "type": "file_selection",
                "requested_action": "summarize_file",
                "results": [
                    {"position": 1, "path": str(target)},
                    {"position": 2, "path": str(target.with_name("beta.md"))},
                ],
            }
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                session=session,
            )

            response = coordinator.respond("9")

            self.assertIn("Choose a number from 1 through 2", response)
            self.assertEqual(fake_llm.calls, [])

    def test_invalid_selection_trace_uses_parsed_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "alpha.md"
            target.write_text("alpha body", encoding="utf-8")
            fake_llm = FakeLLM()
            session = ConversationSession(max_messages=4)
            session.pending_interaction = {
                "type": "file_selection",
                "requested_action": "summarize_file",
                "results": [{"position": 1, "path": str(target)}],
            }
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                session=session,
            )

            class FakeRequest:
                intent = "select_pending_result"
                requires_tool = True
                response_allowed = False
                target = {"selection": "abc"}
                scope = {}
                filters = {}

            with patch.object(coordinator.request_pipeline, "build_request", return_value=FakeRequest()):
                coordinator.respond("abc")

            trace_path = Path(tmpdir) / "request_trace.jsonl"
            self.assertTrue(trace_path.exists())
            payload = trace_path.read_text(encoding="utf-8")
            self.assertIn('"selection": 0', payload)

    def test_pending_state_is_saved_through_session_manager(self) -> None:
        repository = InMemorySessionRepository()
        manager = SessionManager(repository)
        session = manager.start_session(title="demo")
        session.pending_interaction = {"type": "file_selection", "results": [{"position": 1, "path": "a.md"}]}
        fake_llm = FakeLLM()
        coordinator = AssistantCoordinator(
            assistant_name="Iris",
            memory_store=MemoryStoreStub(),
            config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}},
            ollama_client=fake_llm,
            session_manager=manager,
        )

        coordinator._persist_and_return("hi", "done")

        reloaded = repository.get_session(session.id or "")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.pending_interaction["results"][0]["position"], 1)

    def test_count_uses_requested_extension_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "AI"
            root.mkdir(parents=True, exist_ok=True)
            (root / "one.py").write_text("print('x')", encoding="utf-8")
            (root / "two.md").write_text("ignore", encoding="utf-8")
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                    "roots": [{"name": "AI", "path": str(root)}],
                },
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            response = coordinator.respond("Count .py files in the AI folder")

            self.assertIn("Found 1", response)
            self.assertEqual(fake_llm.calls, [])

    def test_count_trace_marks_partial_status_when_subdirectories_are_inaccessible(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "AI"
            root.mkdir(parents=True, exist_ok=True)
            (root / "one.md").write_text("x", encoding="utf-8")
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                    "roots": [{"name": "AI", "path": str(root)}],
                },
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            def fake_walk(path: str, onerror: object | None = None) -> list[tuple[str, list[str], list[str]]]:
                if onerror is not None:
                    onerror(PermissionError("blocked subdirectory"))
                return [(str(path), [], ["one.md"])]

            with patch("core.assistant.coordinator.os.walk", side_effect=fake_walk):
                coordinator.respond("Count .md files in the AI folder")

            trace_path = Path(tmpdir) / "request_trace.jsonl"
            self.assertTrue(trace_path.exists())
            payload = trace_path.read_text(encoding="utf-8")
            self.assertIn('"status": "partial"', payload)

    def test_search_truncation_reports_total_and_exact_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "AI"
            root.mkdir(parents=True, exist_ok=True)
            (root / "phase 5.md").write_text("exact", encoding="utf-8")
            (root / "phase 5 notes.md").write_text("suffix", encoding="utf-8")
            (root / "partial phase 5 archive.md").write_text("partial", encoding="utf-8")
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                    "roots": [{"name": "AI", "path": str(root)}],
                    "search_result_limit": 2,
                },
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            with patch("core.assistant.coordinator.os.walk", return_value=[(str(root), [], ["partial phase 5 archive.md", "phase 5 notes.md", "phase 5.md"])]):
                response = coordinator.respond("Find phase 5")

            self.assertIn("Showing the first 2 of 3", response)
            self.assertIn("phase 5.md", response)
            self.assertIn("partial phase 5 archive.md", response)
            self.assertEqual(fake_llm.calls, [])

    def test_find_files_reports_partial_traversal_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "AI"
            root.mkdir(parents=True, exist_ok=True)
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                    "roots": [{"name": "AI", "path": str(root)}],
                },
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            def fake_walk(path: str, onerror: object | None = None) -> list[tuple[str, list[str], list[str]]]:
                if onerror is not None:
                    onerror(PermissionError("blocked subdirectory"))
                return [(str(path), ["blocked"], [])]

            with patch("core.assistant.coordinator.os.walk", side_effect=fake_walk):
                response = coordinator.respond("Find alpha")

            self.assertIn("subdirectory access error", response.lower())
            self.assertEqual(fake_llm.calls, [])

    def test_context_builder_only_runs_once_for_normal_responses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_llm = RespondIntentLLM()
            builder = CountingContextBuilder()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                context_builder=builder,
                session=ConversationSession(max_messages=4),
            )

            coordinator.respond("hello")

            self.assertEqual(builder.calls, 1)

    def test_coordinator_clarifies_when_decision_output_is_not_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_llm = FakeLLM()
            session = ConversationSession(max_messages=4)
            session.metadata["active_capability"] = "weather"
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                session=session,
            )
            weather_router = WeatherRouterStub()
            coordinator.general_knowledge_router = weather_router

            response = coordinator.respond("how about the rest of the week?")

            self.assertIn("could not determine the right capability", response)
            self.assertEqual(len(fake_llm.calls), 1)
            self.assertEqual(weather_router.route_provider_calls, [])
            self.assertEqual(session.metadata.get("active_capability"), "weather")

    def test_coordinator_clarifies_when_decision_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_llm = FakeLLM()
            session = ConversationSession(max_messages=4)
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                },
                ollama_client=fake_llm,
                session=session,
            )

            class NoRouteLegacyStub(WeatherRouterStub):
                def route(self, text: str):
                    self.route_provider_calls.append(("legacy_route", text))
                    return self.route_provider("weather", text)

            router_stub = NoRouteLegacyStub()
            coordinator.general_knowledge_router = router_stub

            response = coordinator.respond("how about the rest of the week?")

            self.assertIn("could not determine the right capability", response)
            self.assertEqual(router_stub.route_provider_calls, [])

    def test_active_weather_capability_persists_after_non_tool_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = RespondIntentLLM()
            session = ConversationSession(max_messages=4)
            session.metadata["active_capability"] = "weather"
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=llm,
                session=session,
            )
            coordinator.general_knowledge_router = WeatherRouterStub()

            response = coordinator.respond("thanks")

            self.assertIsInstance(response, str)
            self.assertEqual(session.metadata.get("active_capability"), "weather")

    def test_coordinator_executes_typed_weather_capability_request_when_llm_interprets_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = WeatherIntentLLM()
            session = ConversationSession(max_messages=4)
            session.metadata["active_capability"] = "weather"
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=llm,
                session=session,
            )
            weather_router = WeatherRouterStub()
            coordinator.general_knowledge_router = weather_router

            response = coordinator.respond("how about the rest of the week?")

            self.assertIn("Current weather in Anderson, SC", response)
            self.assertEqual(weather_router.execute_request_calls, [("weather", {"location": "Anderson, SC", "range_name": "week", "start": "today", "granularity": "daily"})])
            self.assertEqual(weather_router.route_provider_calls, [("weather", "typed")])

    def test_coordinator_executes_typed_stock_capability_request_when_llm_interprets_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = StockIntentLLM()
            session = ConversationSession(max_messages=4)
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=llm,
                session=session,
            )
            router_stub = WeatherRouterStub()
            coordinator.general_knowledge_router = router_stub

            response = coordinator.respond("what is NVDA trading at?")

            self.assertIn("NVDA:", response)
            self.assertEqual(router_stub.execute_request_calls, [("stocks", {"ticker": "NVDA"})])
            self.assertEqual(router_stub.route_provider_calls, [("stocks", "typed")])
            self.assertEqual(session.metadata.get("active_capability"), "stocks")

    def test_coordinator_immediate_tool_response_prefers_structured_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = StockIntentLLM()
            session = ConversationSession(max_messages=4)
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=llm,
                session=session,
            )

            class FactsRouterStub(WeatherRouterStub):
                def route_provider(self, provider_name: str, text: str):
                    self.route_provider_calls.append((provider_name, text))
                    return type(
                        "Result",
                        (),
                        {
                            "provider": "stocks",
                            "response": "legacy provider prose should not be returned",
                            "fallback_notice": None,
                            "detail_type": "markdown",
                            "detail_title": "Stock Quote - NVDA",
                            "detail_content": "legacy provider prose should not be used for details when facts exist",
                            "metadata": {
                                "capability": "stocks",
                                "facts": {"ticker": "NVDA", "price": 102.0, "change": 2.0, "percent_change": 2.0},
                            },
                        },
                    )()

            router_stub = FactsRouterStub()
            coordinator.general_knowledge_router = router_stub

            response = coordinator.respond("what is NVDA trading at?")

            self.assertEqual(response, "NVDA: $102.00 (+2.00, +2.00%)")
            self.assertNotIn("legacy provider prose", response)

    def test_acceptance_gate_orchestrator_turns_complete_without_deprecated_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            weather_session = ConversationSession(max_messages=4)
            weather_coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                },
                ollama_client=WeatherIntentLLM(),
                session=weather_session,
            )
            weather_coordinator.general_knowledge_router = WeatherRouterStub()

            weather_response = weather_coordinator.respond("what is it like outside?")

            self.assertIn("Current weather in Anderson, SC", weather_response)

            stock_session = ConversationSession(max_messages=4)
            stock_coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                },
                ollama_client=StockIntentLLM(),
                session=stock_session,
            )
            stock_coordinator.general_knowledge_router = WeatherRouterStub()

            stock_response = stock_coordinator.respond("what is NVDA trading at?")

            self.assertIn("NVDA:", stock_response)

    def test_acceptance_gate_cross_topic_switch_preserves_calendar_follow_up_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session = ConversationSession(max_messages=8)
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                },
                ollama_client=CrossTopicIntentLLM(),
                session=session,
            )
            coordinator.general_knowledge_router = CrossTopicRouterStub()

            weather_response = coordinator.respond("What is tomorrow's forecast?")
            self.assertIn("Current weather in Anderson, SC", weather_response)
            self.assertEqual(session.metadata.get("active_capability"), "weather")

            calendar_response = coordinator.respond("What meetings do I have tomorrow?")
            self.assertIn("meetings tomorrow", calendar_response)
            self.assertEqual(session.metadata.get("active_capability"), "calendar")

            afternoon_response = coordinator.respond("What about the afternoon?")
            self.assertIn("meetings tomorrow afternoon", afternoon_response)
            self.assertEqual(session.metadata.get("active_capability"), "calendar")

    def test_acceptance_gate_required_scenarios_route_without_phrase_parser_or_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session = ConversationSession(max_messages=12)
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={
                    "assistant_name": "Iris",
                    "conversation": {"recent_message_limit": 8, "summary_trigger_message_count": 24},
                    "audit_path": tmpdir,
                },
                ollama_client=PhaseAcceptanceIntentLLM(),
                session=session,
            )
            router_stub = PhaseAcceptanceRouterStub()
            coordinator.general_knowledge_router = router_stub
            coordinator.request_pipeline = PassThroughRespondPipeline()

            weather_current = coordinator.respond("What is it like outside?")
            self.assertIn("Current weather in Anderson, SC", weather_current)
            weather_tomorrow = coordinator.respond("What about tomorrow?")
            self.assertIn("Tomorrow in Anderson, SC", weather_tomorrow)
            weather_weekend = coordinator.respond("How does the weekend look?")
            self.assertIn("Weekend in Anderson, SC", weather_weekend)
            weather_mow = coordinator.respond("Would Saturday be a good day to mow?")
            self.assertIn("mowing conditions are good", weather_mow)

            file_search = coordinator.respond("Find the spreadsheet I made about E*TRADE last month.")
            self.assertIn("Found matching files", file_search)
            file_open = coordinator.respond("Open the second one.")
            self.assertIn("Opened ETRADE_Trades.xlsx", file_open)
            file_remove = coordinator.respond("No, remove that one from the results.")
            self.assertIn("Removed ETRADE_Trades.xlsx", file_remove)
            file_other = coordinator.respond("What was the other Excel file?")
            self.assertIn("other Excel file", file_other)

            calendar_day = coordinator.respond("What do I have tomorrow?")
            self.assertIn("Tomorrow you have two meetings", calendar_day)
            calendar_afternoon = coordinator.respond("What about the afternoon?")
            self.assertIn("Tomorrow afternoon", calendar_afternoon)
            calendar_move = coordinator.respond("Move the later one to Friday.")
            self.assertIn("Moved the later meeting to Friday", calendar_move)

            cross_weather = coordinator.respond("What is tomorrow's forecast?")
            self.assertIn("Tomorrow in Anderson, SC", cross_weather)
            self.assertEqual(session.metadata.get("active_capability"), "weather")
            cross_calendar = coordinator.respond("What meetings do I have tomorrow?")
            self.assertIn("Tomorrow you have two meetings", cross_calendar)
            self.assertEqual(session.metadata.get("active_capability"), "calendar")
            cross_follow_up = coordinator.respond("What about the afternoon?")
            self.assertIn("Tomorrow afternoon", cross_follow_up)
            self.assertEqual(session.metadata.get("active_capability"), "calendar")

            self.assertEqual(len(router_stub.execute_request_calls), 14)


if __name__ == "__main__":
    unittest.main()
