# File: core/tests/test_sweep_sections.py

"""Section sweep: conversation search and fork (2.5), offline roots (2.6), retention (2.8)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.assistant.session_commands import SessionCommandHandler
from core.audit.retention import apply_retention, trim_jsonl
from core.conversation.session_manager import SessionManager
from core.conversation.session_repository import SessionRepository
from core.documents.roots import probe_root, probe_roots
from core.scheduler.jobs import AUDIT_RETENTION, audit_retention, build_jobs


class SessionSearchAndForkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = SessionRepository(Path(self.tempdir.name), assistant_name="Iris", model="qwen")
        self.manager = SessionManager(self.repository)
        self.lines: list[str] = []
        self.handler = SessionCommandHandler(self.manager, self.repository, output=lambda text, role=None: self.lines.append(text))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, command: str, state: dict | None = None) -> str:
        del self.lines[:]
        self.assertTrue(self.handler.handle(command, state or {}))
        return "\n".join(self.lines)

    def test_search_finds_messages_across_sessions(self) -> None:
        first = self.manager.start_session(title="Dispatch work")
        self.manager.add_message("user", "How do I hide the driver column on the pool page?")
        self.manager.add_message("assistant", "Use a page extension and set Visible to false.")
        self.manager.close_active_session()
        second = self.manager.start_session(title="Trading")
        self.manager.add_message("user", "Summarise the candidates from this morning")
        found = self._run("/session search driver column")
        self.assertIn("1 message mention 'driver column'", found)
        self.assertIn(first.id, found)
        self.assertIn("Dispatch work", found)
        self.assertNotIn(second.id, found)
        self.assertIn("Nothing in past conversations", self._run("/session search nothing here"))
        self.assertTrue(self._run("/session search").startswith("Usage"))

    def test_fork_copies_the_conversation_and_keeps_the_original(self) -> None:
        original = self.manager.start_session(title="Dispatch work", project_id="mammoth")
        self.manager.add_message("user", "first question")
        self.manager.add_message("assistant", "first answer")
        forked = self._run("/session fork Try the other approach")
        self.assertIn(f"Forked {original.id} into", forked)
        active = self.manager.get_active_session()
        self.assertNotEqual(active.id, original.id)
        self.assertEqual(active.title, "Try the other approach")
        self.assertEqual(active.project_id, "mammoth")
        self.assertEqual([item["content"] for item in active.get_messages()], ["first question", "first answer"])
        self.assertEqual(active.metadata["forked_from"], original.id)
        self.manager.add_message("user", "second question")
        kept = self.repository.get_session(original.id)
        self.assertEqual(len(kept.get_messages()), 2)
        self.assertEqual(len(self.repository.get_session(active.id).get_messages()), 3)
        self.manager.close_active_session()
        self.assertIn("No active session to fork", self._run("/session fork"))


class RootProbeTests(unittest.TestCase):
    def test_present_missing_and_slow_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            present = probe_root(tempdir)
            self.assertTrue(present.online)
            missing = probe_root(Path(tempdir) / "nope")
            self.assertFalse(missing.online)
            self.assertIn("not found", missing.reason)
            statuses = probe_roots([tempdir, Path(tempdir) / "nope"])
            self.assertEqual([item.online for item in statuses], [True, False])
            self.assertIn("(online)", statuses[0].label)

    def test_a_root_that_hangs_is_reported_within_the_timeout(self) -> None:
        def slow(_self) -> bool:
            time.sleep(2.0)
            return True

        started = time.perf_counter()
        with patch.object(type(Path()), "is_dir", slow):
            status = probe_root(".", timeout=0.2)
        self.assertFalse(status.online)
        self.assertIn("did not answer within", status.reason)
        self.assertLess(time.perf_counter() - started, 1.0)


class RetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.folder = Path(self.tempdir.name)
        now = datetime.now(timezone.utc)
        self.now = now
        old = (now - timedelta(days=120)).isoformat()
        fresh = (now - timedelta(days=3)).isoformat()
        self.audit = self.folder / "audit.jsonl"
        self.audit.write_text("\n".join([json.dumps({"created_at": old, "event": "old"}), "not json at all", json.dumps({"created_at": fresh, "event": "fresh"}), json.dumps({"event": "undated"})]) + "\n", encoding="utf-8")
        captures = self.folder / "Captures"
        captures.mkdir()
        old_capture = captures / "cam.jpg"
        old_capture.write_bytes(b"x")
        stamp = (now - timedelta(days=200)).timestamp()
        import os

        os.utime(old_capture, (stamp, stamp))
        (captures / "recent.jpg").write_bytes(b"y")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_old_dated_lines_go_and_undated_or_unparseable_lines_stay(self) -> None:
        before, after = trim_jsonl(self.audit, keep_days=90, now=self.now)
        self.assertEqual((before, after), (4, 3))
        kept = self.audit.read_text(encoding="utf-8")
        self.assertNotIn('"old"', kept)
        self.assertIn('"fresh"', kept)
        self.assertIn("not json at all", kept)
        self.assertIn('"undated"', kept)
        self.assertEqual(trim_jsonl(self.folder / "missing.jsonl"), (0, 0))

    def test_the_job_trims_files_and_removes_old_captures(self) -> None:
        report = apply_retention([self.audit], keep_days=90, capture_folders=[self.folder / "Captures"], now=self.now)
        self.assertEqual(report.removed, 2)
        self.assertIn("audit.jsonl: 1 of 4 lines older than 90 days removed", report.summary())
        self.assertIn("1 captured file removed", report.summary())
        self.assertTrue((self.folder / "Captures" / "recent.jpg").exists())
        job = audit_retention(lambda: [self.audit], capture_folders=lambda: [self.folder / "Captures"], keep_days=90)
        result = job({"keep_days": 90})
        self.assertTrue(result.ok)
        self.assertIn("Nothing older than 90 days", result.summary)
        jobs = build_jobs(retention=job)
        self.assertIn(AUDIT_RETENTION, jobs)


if __name__ == "__main__":
    unittest.main()
