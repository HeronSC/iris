# File: core/test_phase2.py

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import ollama

from core.assistant.coordinator import AssistantCoordinator
from core.assistant.project_command import ProjectCommandHandler
from core.assistant.save_command import SaveCommandHandler
from core.assistant.session_commands import SessionCommandHandler
from core.assistant.llm_client import LLMClient
from core.conversation.context_builder import ContextBuilder
from core.conversation.session import ConversationSession
from core.conversation.session_manager import SessionManager
from core.conversation.session_repository import SessionRepository
from core.conversation.session_summarizer import SessionSummarizer
from core.llm.models import LLMRequest, ToolSpec
from core.llm.ollama_client import OllamaClient, OllamaClientError
from core.profile.store import MemoryStore
from core.profile.writer import MemoryWriter


class ConversationSessionTests(unittest.TestCase):
    def test_session_keeps_recent_messages_only(self) -> None:
        session = ConversationSession(max_messages=2)
        session.add_message("user", "first")
        session.add_message("assistant", "second")
        session.add_message("user", "third")

        messages = session.get_messages()

        self.assertEqual([item["content"] for item in messages], ["second", "third"])


class OllamaClientTests(unittest.TestCase):
    def _client(self) -> OllamaClient:
        return OllamaClient("http://127.0.0.1:11434", "qwen")

    def test_generate_parses_successful_response(self) -> None:
        client = self._client()
        raw = SimpleNamespace(
            model="qwen",
            done_reason="stop",
            prompt_eval_count=12,
            eval_count=3,
            total_duration=2_000_000,
            load_duration=0,
            message=SimpleNamespace(content="hello", thinking=None, tool_calls=None),
        )
        with patch.object(client._client, "chat", return_value=raw):
            self.assertEqual(client.generate("system", "user"), "hello")
        self.assertIsNotNone(client.last_response)
        self.assertEqual(client.last_response.usage.prompt_tokens, 12)
        self.assertEqual(client.last_response.usage.completion_tokens, 3)
        self.assertEqual(client.last_response.usage.total_duration_ms, 2.0)

    def test_chat_returns_tool_calls_with_parsed_arguments(self) -> None:
        client = self._client()
        raw = SimpleNamespace(
            model="qwen",
            done_reason="stop",
            message=SimpleNamespace(
                content="",
                thinking=None,
                tool_calls=[
                    SimpleNamespace(function=SimpleNamespace(name="launch_application", arguments={"app_name": "code"})),
                    SimpleNamespace(function=SimpleNamespace(name="open_url", arguments='{"shortcut": "github"}')),
                ],
            ),
        )
        with patch.object(client._client, "chat", return_value=raw) as chat:
            request = LLMRequest.from_prompts(
                "system",
                "user",
                tools=(ToolSpec(name="launch_application", description="Launch", parameters={"type": "object"}),),
                think=False,
            )
            response = client.chat(request)
        self.assertTrue(response.has_tool_calls)
        self.assertEqual(response.tool_calls[0].name, "launch_application")
        self.assertEqual(response.tool_calls[0].arguments, {"app_name": "code"})
        self.assertEqual(response.tool_calls[1].arguments, {"shortcut": "github"})
        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["model"], "qwen")
        self.assertEqual(kwargs["think"], False)
        self.assertEqual(kwargs["tools"][0]["function"]["name"], "launch_application")
        self.assertEqual([m["role"] for m in kwargs["messages"]], ["system", "user"])

    def test_generate_report_connection_failures(self) -> None:
        client = self._client()
        with patch.object(client._client, "chat", side_effect=httpx.ConnectError("boom")):
            with self.assertRaises(OllamaClientError) as error:
                client.generate("system", "user")
        self.assertIn("connection failed", str(error.exception))

    def test_generate_reports_timeouts(self) -> None:
        client = self._client()
        with patch.object(client._client, "chat", side_effect=httpx.ReadTimeout("slow")):
            with self.assertRaises(OllamaClientError) as error:
                client.generate("system", "user")
        self.assertIn("timed out", str(error.exception))

    def test_generate_reports_empty_response(self) -> None:
        client = self._client()
        raw = SimpleNamespace(model="qwen", message=SimpleNamespace(content="", thinking=None, tool_calls=None))
        with patch.object(client._client, "chat", return_value=raw):
            with self.assertRaises(OllamaClientError):
                client.generate("system", "user")

    def test_generate_reports_http_error_detail(self) -> None:
        client = self._client()
        with patch.object(client._client, "chat", side_effect=ollama.ResponseError("model not found", 404)):
            with self.assertRaises(OllamaClientError) as error:
                client.generate("system", "user")

        self.assertIn("HTTP 404", str(error.exception))
        self.assertIn("model not found", str(error.exception))


class ContextBuilderTests(unittest.TestCase):
    def test_context_builder_includes_session_summary_and_recent_messages(self) -> None:
        store = MemoryStore(
            {
                "profile": {"profile": {}},
                "preferences": {"preferences": []},
                "projects": {"projects": []},
                "knowledge": {"knowledge_areas": []},
            }
        )
        builder = ContextBuilder("Iris", store)
        context = builder.build_context(
            "What did we decide?",
            session_summary="We are refining the CLI flow.",
            recent_messages=[{"role": "user", "content": "Start a session"}, {"role": "assistant", "content": "Done"}],
        )

        self.assertIn("Session summary:", context)
        self.assertIn("We are refining the CLI flow.", context)
        self.assertIn("Recent conversation:", context)
        self.assertIn("user: Start a session", context)
        self.assertIn("assistant: Done", context)


class MemoryWriterTests(unittest.TestCase):
    def test_writer_saves_json_with_indentation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = MemoryWriter(tmpdir)
            path = Path(tmpdir) / "profile.json"
            writer.save_profile({"name": "Iris"})

            self.assertTrue(path.exists())
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["name"], "Iris")

    def test_writer_requires_existing_memory_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing"
            with self.assertRaises(FileNotFoundError):
                MemoryWriter(missing)


class SaveCommandTests(unittest.TestCase):
    def test_save_persists_full_memory_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_data = {
                "profile": {"profile": {"display_name": "Henry"}},
                "preferences": {"preferences": [{"id": "x"}]},
                "projects": {"projects": [{"id": "iris"}]},
                "knowledge": {"knowledge_areas": [{"id": "azure"}]},
            }
            store = MemoryStore(memory_data)
            writer = MemoryWriter(tmpdir)
            handler = SaveCommandHandler()

            handled = handler.handle("/save", {"store": store, "writer": writer})

            self.assertTrue(handled)
            self.assertEqual(
                json.loads((Path(tmpdir) / "profile.json").read_text(encoding="utf-8")),
                memory_data["profile"],
            )
            self.assertEqual(
                json.loads((Path(tmpdir) / "preferences.json").read_text(encoding="utf-8")),
                memory_data["preferences"],
            )
            self.assertEqual(
                json.loads((Path(tmpdir) / "projects.json").read_text(encoding="utf-8")),
                memory_data["projects"],
            )
            self.assertEqual(
                json.loads((Path(tmpdir) / "knowledge.json").read_text(encoding="utf-8")),
                memory_data["knowledge"],
            )

    def test_save_requires_exact_command(self) -> None:
        handler = SaveCommandHandler()

        self.assertFalse(handler.handle("/save now", {}))
        self.assertFalse(handler.handle("/saveanything", {}))


class SessionCommandTests(unittest.TestCase):
    def test_session_summarize_command_updates_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            manager = SessionManager(repository)
            handler = SessionCommandHandler(manager, repository)
            session = manager.start_session(title="Demo")
            session.add_message("user", "hello")
            session.add_message("assistant", "hi")

            handled = handler.handle("/session summarize", {"active_project_id": None})

            self.assertTrue(handled)
            self.assertIn("Topic:", repository.get_session(session.id).summary)
            self.assertIn("Message count in session", repository.get_session(session.id).summary)

    def test_session_resume_syncs_active_project_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            manager = SessionManager(repository)
            original = manager.start_session(title="Demo", project_id="iris")
            manager.close_active_session()
            handler = SessionCommandHandler(manager, repository)
            state = {"active_project_id": None}

            handled = handler.handle(f"/session resume {original.id}", state)

            self.assertTrue(handled)
            self.assertEqual(state["active_project_id"], "iris")


class SessionMessageMetadataTests(unittest.TestCase):
    def test_session_message_includes_id_and_timestamp(self) -> None:
        session = ConversationSession(max_messages=None)
        session.add_message("user", "hello")

        messages = session.get_messages()

        self.assertEqual(messages[0]["id"], "msg-000001")
        self.assertIn("created_at", messages[0])


class ProjectCommandTests(unittest.TestCase):
    def test_project_command_is_case_insensitive_and_stores_canonical_id(self) -> None:
        store = MemoryStore(
            {
                "profile": {},
                "preferences": {"preferences": []},
                "projects": {
                    "projects": [
                        {"id": "iris", "name": "Iris", "status": "active"},
                    ]
                },
                "knowledge": {"knowledge_areas": []},
            }
        )
        state = {"store": store, "active_project_id": None}
        handler = ProjectCommandHandler()

        handled = handler.handle("/project IRIS", state)

        self.assertTrue(handled)
        self.assertEqual(state["active_project_id"], "iris")

    def test_project_list_command_prints_projects(self) -> None:
        store = MemoryStore(
            {
                "profile": {},
                "preferences": {"preferences": []},
                "projects": {
                    "projects": [
                        {"id": "iris", "name": "Iris", "status": "active"},
                    ]
                },
                "knowledge": {"knowledge_areas": []},
            }
        )
        state = {"store": store, "active_project_id": None}
        handler = ProjectCommandHandler()

        output = StringIO()
        with redirect_stdout(output):
            handled = handler.handle("/project list", state)

        self.assertTrue(handled)
        self.assertIn("iris", output.getvalue().lower())


class CoordinatorSummaryTriggerTests(unittest.TestCase):
    def test_summary_updates_when_threshold_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            manager = SessionManager(repository)
            manager.start_session(title="Demo")
            store = MemoryStore(
                {
                    "profile": {"profile": {}},
                    "preferences": {"preferences": []},
                    "projects": {"projects": []},
                    "knowledge": {"knowledge_areas": []},
                }
            )
            client = Mock(spec=LLMClient)
            # First call is the orchestrator decision, second is the reply itself.
            client.generate.side_effect = ['{"decision": "respond"}', "ok"]
            coordinator = AssistantCoordinator(
                "Iris",
                store,
                {"conversation": {"summary_trigger_message_count": 2}},
                ollama_client=client,
                session_manager=manager,
            )

            coordinator.respond("hello")

            active = manager.get_active_session()
            self.assertIsNotNone(active)
            self.assertIn("Topic:", active.summary)

    def test_failed_generation_preserves_user_message_and_records_error_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            manager = SessionManager(repository)
            manager.start_session(title="Demo")
            store = MemoryStore(
                {
                    "profile": {"profile": {}},
                    "preferences": {"preferences": []},
                    "projects": {"projects": []},
                    "knowledge": {"knowledge_areas": []},
                }
            )
            client = Mock(spec=LLMClient)
            # Let the orchestrator decision succeed so the failure lands on generation.
            client.generate.side_effect = ['{"decision": "respond"}', OllamaClientError("Connection refused")]
            coordinator = AssistantCoordinator(
                "Iris",
                store,
                {"conversation": {"summary_trigger_message_count": 2}},
                ollama_client=client,
                session_manager=manager,
            )

            with self.assertRaises(OllamaClientError):
                coordinator.respond("hello")

            active = manager.get_active_session()
            self.assertIsNotNone(active)
            messages = active.get_messages()
            self.assertEqual(messages[0]["role"], "user")
            self.assertEqual(messages[0]["content"], "hello")
            self.assertEqual(messages[1]["role"], "system")
            self.assertEqual(messages[1]["content"], "Assistant response failed.")
            self.assertEqual(messages[1].get("metadata", {}).get("error_type"), "OllamaClientError")

    def test_orchestration_failure_records_user_message_and_error_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            manager = SessionManager(repository)
            manager.start_session(title="Demo")
            store = MemoryStore(
                {
                    "profile": {"profile": {}},
                    "preferences": {"preferences": []},
                    "projects": {"projects": []},
                    "knowledge": {"knowledge_areas": []},
                }
            )
            client = Mock(spec=LLMClient)
            client.generate.side_effect = OllamaClientError("Connection refused")
            coordinator = AssistantCoordinator(
                "Iris",
                store,
                {"conversation": {"summary_trigger_message_count": 2}},
                ollama_client=client,
                session_manager=manager,
            )

            with self.assertRaises(OllamaClientError):
                coordinator.respond("hello")

            active = manager.get_active_session()
            self.assertIsNotNone(active)
            messages = active.get_messages()
            self.assertEqual(messages[0]["role"], "user")
            self.assertEqual(messages[0]["content"], "hello")
            self.assertEqual(messages[1]["role"], "system")
            self.assertEqual(messages[1]["content"], "Assistant response failed.")
            self.assertEqual(messages[1].get("metadata", {}).get("error_type"), "OllamaClientError")


if __name__ == "__main__":
    unittest.main()

