# File: core/test_phase4_8.py

import tempfile
import unittest
from pathlib import Path

from core.assistant.coordinator import AssistantCoordinator
from core.conversation.request_pipeline import RequestPipeline
from core.conversation.session import ConversationSession


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

    def generate(self, system_prompt: str, user_prompt: str, task: str | None = None) -> str:
        self.calls.append((system_prompt, user_prompt))
        return "summary-ready"


class PhaseFourPointEightTests(unittest.TestCase):
    def test_request_pipeline_detects_explicit_file_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"audit_path": tmpdir, "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}}
            pipeline = RequestPipeline(config=config)
            request = pipeline.build_request("summarize this file E:/AI/Iris/phase 4.75.md", state={})

            self.assertTrue(request.requires_tool)
            self.assertEqual(request.intent, "read_file")
            self.assertEqual(request.target["path"], "E:/AI/Iris/phase 4.75.md")

    def test_request_pipeline_detects_posix_explicit_file_paths(self) -> None:
        pipeline = RequestPipeline(config={})

        request = pipeline.build_request("summarize this file /tmp/example/notes.md", state={})

        self.assertTrue(request.requires_tool)
        self.assertEqual(request.intent, "read_file")
        self.assertEqual(request.target["path"], "/tmp/example/notes.md")

    def test_coordinator_reads_explicit_file_before_asking_llm(self) -> None:
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

            response = coordinator.respond(f"summarize this file {target}")

            self.assertEqual(response, "No approved file roots are configured.")
            self.assertFalse(fake_llm.calls)

    def test_coordinator_reports_missing_file_without_chat_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_llm = FakeLLM()
            coordinator = AssistantCoordinator(
                assistant_name="Iris",
                memory_store=MemoryStoreStub(),
                config={"assistant_name": "Iris", "conversation": {"recent_message_limit": 4, "summary_trigger_message_count": 24}, "audit_path": tmpdir},
                ollama_client=fake_llm,
                session=ConversationSession(max_messages=4),
            )

            response = coordinator.respond("summarize this file E:/AI/does-not-exist.md")

            self.assertIn("no approved file roots are configured", response.lower())
            self.assertFalse(fake_llm.calls)


if __name__ == "__main__":
    unittest.main()
