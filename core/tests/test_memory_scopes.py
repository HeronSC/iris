# File: core/tests/test_memory_scopes.py

"""Memory scope (2.1): global, one project, or one session; a new project inherits only the global records."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.assistant.knowledge_commands import KnowledgeCommandHandler
from core.assistant.knowledge_provider import KnowledgeRecallProvider, RecallRequest
from core.knowledge.graph import KnowledgeGraph
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.models import MemoryKind, MemoryRecord
from core.knowledge.repository import KnowledgeRepository
from core.knowledge.retrieval import KnowledgeQuery, KnowledgeRetriever
from core.knowledge.review import KnowledgeReviewWorkflow
from core.knowledge.schema import SCHEMA_VERSION, ensure_schema, stored_version
from core.knowledge.scopes import describe_scope, resolve_scope, split_scope_flag, visible_scopes
from core.storage.sqlite_database import SQLiteDatabase


class ScopeHelperTests(unittest.TestCase):
    def test_visible_scopes_and_resolution(self) -> None:
        self.assertEqual(visible_scopes(), ("global",))
        self.assertEqual(visible_scopes("mammoth", "s1"), ("global", "project:mammoth", "session:s1"))
        self.assertEqual(resolve_scope(None), "global")
        self.assertEqual(resolve_scope("project", project_id="mammoth"), "project:mammoth")
        self.assertEqual(resolve_scope("session", session_id="s1"), "session:s1")
        with self.assertRaises(ValueError):
            resolve_scope("project")
        with self.assertRaises(ValueError):
            resolve_scope("galaxy")
        self.assertEqual(describe_scope("project:mammoth"), "project mammoth")
        self.assertEqual(describe_scope(None), "global")
        self.assertEqual(split_scope_flag("@project trading/x it rained"), ("project", "trading/x it rained"))
        self.assertEqual(split_scope_flag("trading/x it rained"), (None, "trading/x it rained"))


class SchemaAndRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "knowledge.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_an_old_database_gains_the_scope_column_and_keeps_its_rows(self) -> None:
        connection = sqlite3.connect(str(self.path))
        connection.executescript(
            """
            CREATE TABLE knowledge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO knowledge_meta VALUES ('schema_version', '2');
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL, topic TEXT NOT NULL,
                status TEXT NOT NULL, content TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', confidence REAL,
                source TEXT NOT NULL, source_ref TEXT, occurred_at TEXT, created_at TEXT NOT NULL,
                supersedes TEXT, superseded_by TEXT
            );
            INSERT INTO memories (id, sequence, kind, topic, status, content, source, created_at)
            VALUES ('old1', 1, 'fact', 'iris/setup', 'accepted', 'Iris runs locally', 'user', '2026-09-01T00:00:00+00:00');
            """
        )
        connection.commit()
        connection.close()
        database = SQLiteDatabase(self.path)
        ensure_schema(database)
        self.assertEqual(stored_version(database), SCHEMA_VERSION)
        repository = KnowledgeRepository(database)
        old = repository.get("old1")
        self.assertEqual(old.scope, "global")
        self.assertTrue(self.path.with_name(f"knowledge.before-v{SCHEMA_VERSION}.db").exists())

    def test_records_keep_their_scope_through_storage_and_supersession(self) -> None:
        repository = KnowledgeRepository(SQLiteDatabase(self.path))
        stored = repository.add(MemoryRecord(kind=MemoryKind.FACT, topic="mammoth/dispatch", content="Pool page is 99510", source="user", scope="project:mammoth"))
        self.assertEqual(repository.get(stored.id).scope, "project:mammoth")
        replacement = repository.supersede(stored.id, MemoryRecord(kind=MemoryKind.FACT, topic="mammoth/dispatch", content="Pool page is 99511", source="user", scope="project:mammoth"))
        self.assertEqual(repository.get(replacement.id).scope, "project:mammoth")
        repository.add(MemoryRecord(kind=MemoryKind.FACT, topic="iris/setup", content="Iris runs locally", source="user"))
        self.assertEqual(repository.count_by_scope(), {"global": 1, "project:mammoth": 1})


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "knowledge.db")
        self.repository = KnowledgeRepository(self.database)
        self.repository.add_many(
            [
                MemoryRecord(kind=MemoryKind.FACT, topic="dispatch", content="Dispatch pool page uses JSON only", source="user", scope="global"),
                MemoryRecord(kind=MemoryKind.FACT, topic="dispatch", content="Dispatch pool page uses JSON only", source="user", scope="project:mammoth"),
                MemoryRecord(kind=MemoryKind.FACT, topic="dispatch", content="Dispatch pool page uses JSON only", source="user", scope="project:other"),
                MemoryRecord(kind=MemoryKind.FACT, topic="dispatch", content="Dispatch pool page uses JSON only", source="user", scope="session:s1"),
            ]
        )
        self.retriever = KnowledgeRetriever(self.database)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_scopes_filter_candidates_and_the_projects_own_record_ranks_first(self) -> None:
        everything = self.retriever.retrieve(KnowledgeQuery(text="dispatch pool json", limit=10))
        self.assertEqual(len(everything.records), 4)
        mine = self.retriever.retrieve(KnowledgeQuery(text="dispatch pool json", limit=10, scopes=visible_scopes("mammoth", "s1")))
        self.assertEqual(sorted(item.record.scope for item in mine.records), ["global", "project:mammoth", "session:s1"])
        self.assertNotEqual(mine.records[0].record.scope, "global")
        self.assertIn("scope", mine.records[0].reasons)
        self.assertEqual(mine.records[-1].record.scope, "global")
        fresh = self.retriever.retrieve(KnowledgeQuery(text="dispatch pool json", limit=10, scopes=visible_scopes("brand-new")))
        self.assertEqual([item.record.scope for item in fresh.records], ["global"])

    def test_the_recall_provider_asks_for_the_visible_scopes(self) -> None:
        provider = KnowledgeRecallProvider(self.retriever, scopes=lambda: visible_scopes("other"))
        result = provider.execute_detailed_request(RecallRequest(query="dispatch pool json", limit=10))
        self.assertEqual(result.metadata["found"], 2)
        open_provider = KnowledgeRecallProvider(self.retriever)
        self.assertEqual(open_provider.execute_detailed_request(RecallRequest(query="dispatch pool json", limit=10)).metadata["found"], 4)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "knowledge.db")
        self.graph = KnowledgeGraph(self.database)
        self.workflow = KnowledgeReviewWorkflow(HypothesisTracker(self.graph))
        self.lines: list[str] = []
        self.project_id: str | None = "mammoth"
        self.handler = KnowledgeCommandHandler(
            self.workflow,
            output=lambda text, role=None: self.lines.append(text),
            scope_resolver=lambda word: resolve_scope(word, project_id=self.project_id, session_id="s1"),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, command: str) -> str:
        del self.lines[:]
        self.assertTrue(self.handler.handle(command, {}))
        return "\n".join(self.lines)

    def test_observations_default_to_global_and_take_a_scope_flag(self) -> None:
        self.assertIn("(global)", self._run("/knowledge observe dispatch/pool The pool page renders JSON"))
        self.assertIn("(project mammoth)", self._run("/knowledge observe @project dispatch/pool Driver column is hidden"))
        self.assertIn("(session s1)", self._run("/knowledge observe @session dispatch/pool Trying a wider pool"))
        self.assertIn("(project mammoth)", self._run("/knowledge hypothesize @project dispatch/pool Drivers prefer the wide pool"))
        records = self.graph.records.list_all()
        self.assertEqual(sorted(record.scope for record in records), ["global", "project:mammoth", "project:mammoth", "session:s1"])
        project_observation = [record for record in records if record.scope == "project:mammoth" and record.kind is MemoryKind.OBSERVATION][0]
        self._run(f"/knowledge outcome {project_observation.id[:8]} It was hidden by a page extension")
        outcome = [record for record in self.graph.records.list_all() if record.kind is MemoryKind.OUTCOME][0]
        self.assertEqual(outcome.scope, "project:mammoth")
        listing = self._run("/knowledge scopes")
        self.assertIn("- project mammoth: 3", listing)
        self.assertIn("A new project sees only the global records", listing)

    def test_a_project_scope_without_a_project_is_refused(self) -> None:
        self.project_id = None
        self.handler.handle("/knowledge observe @project dispatch/pool Something", {})
        self.assertIn("No project is active", self.lines[-1])
        self.assertEqual(self.graph.records.count(), 0)


if __name__ == "__main__":
    unittest.main()
