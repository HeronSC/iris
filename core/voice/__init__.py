import tempfile
import unittest
import json
from pathlib import Path

from core.conversation.session_manager import SessionManager
from core.conversation.session_repository import SessionRepository


class SessionRepositoryTests(unittest.TestCase):
    def test_create_and_resume_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            manager = SessionManager(repository)

            session = manager.start_session(title="Phase 2", project_id="assistant")
            manager.add_message("user", "Hello")
            manager.add_message("assistant", "Hi there")
            manager.close_active_session()

            resumed = manager.resume_session(session.id)
            self.assertEqual(resumed.title, "Phase 2")
            self.assertEqual(resumed.project_id, "assistant")
            self.assertEqual(resumed.status, "active")
            self.assertEqual(len(resumed.get_messages()), 2)

    def test_list_sessions_filters_by_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            repository.create_session(title="One", project_id="assistant")
            repository.create_session(title="Two", project_id="other")

            sessions = repository.list_sessions(project_id="assistant")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["title"], "One")

    def test_list_sessions_returns_most_recent_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            manager = SessionManager(repository)

            first = manager.start_session(title="First")
            manager.close_active_session()

            second = manager.start_session(title="Second")
            manager.close_active_session()

            resumed_first = manager.resume_session(first.id)
            manager.add_message("user", "touch first")
            manager.close_active_session()

            latest = repository.list_sessions(limit=1)
            self.assertEqual(latest[0]["id"], resumed_first.id)
            self.assertNotEqual(latest[0]["id"], second.id)

    def test_rebuilds_index_when_index_is_corrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = SessionRepository(tmpdir)
            session = repository.create_session(title="Recoverable", project_id="assistant")

            index_path = Path(tmpdir) / "index.json"
            index_path.write_text("{bad json", encoding="utf-8")

            sessions = repository.list_sessions()

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["id"], session.id)
            restored = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(len(restored.get("sessions", [])), 1)

