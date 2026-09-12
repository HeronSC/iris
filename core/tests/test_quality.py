# File: core/tests/test_quality.py

"""Small quality items: a latency budget (12), a readable memory export (2.1), corrections kept with their why (2.2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.assistant.corrections_command import TOPIC, CorrectionsCommandHandler
from core.knowledge import KnowledgeGraph, MemoryKind, MemoryRecord
from core.knowledge.export import export_memory, render_markdown
from core.llm.budget import LatencyBudget
from core.storage.sqlite_database import SQLiteDatabase


class BudgetTests(unittest.TestCase):
    def test_a_slow_class_is_named_and_a_quiet_one_is_not(self) -> None:
        budget = LatencyBudget.from_config({"latency_budget_ms": {"chat": 5000}})
        rows = [
            {"task": "chat", "model": "qwen3:8b", "calls": 10, "avg_wall_ms": 9000.0},
            {"task": "code", "model": "qwen2.5-coder:7b", "calls": 4, "avg_wall_ms": 15000.0},
            {"task": "intent", "model": "qwen3:8b", "calls": 2, "avg_wall_ms": 99000.0},
        ]
        warnings = budget.check(rows)
        self.assertEqual(len(warnings), 1)
        self.assertIn("chat on qwen3:8b averages 9.0 s over 10 calls; its budget is 5.0 s", warnings[0])

    def test_an_unknown_class_uses_the_default(self) -> None:
        self.assertEqual(LatencyBudget().for_task("mystery"), LatencyBudget().for_task(None))


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.graph = KnowledgeGraph(SQLiteDatabase(self.root / "knowledge.db"))
        self.graph.records.add(MemoryRecord(kind=MemoryKind.FACT, topic="iris/design", content="WAL is safe on a local drive", source="henry"))
        old = self.graph.records.add(MemoryRecord(kind=MemoryKind.FACT, topic="trading/rules", content="Old rule", source="henry"))
        self.graph.records.supersede(old.id, MemoryRecord(kind=MemoryKind.FACT, topic="trading/rules", content="New rule", source="henry"))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_export_is_readable_and_leaves_superseded_records_out(self) -> None:
        records = self.graph.records.list_all()
        report = export_memory(records, self.root / "Exports")

        markdown = report.markdown_path.read_text(encoding="utf-8")
        self.assertIn("## iris/design", markdown)
        self.assertIn("WAL is safe on a local drive", markdown)
        self.assertIn("New rule", markdown)
        self.assertNotIn("Old rule", markdown)
        payload = json.loads(report.json_path.read_text(encoding="utf-8"))
        self.assertEqual({item["content"] for item in payload["records"]}, {"WAL is safe on a local drive", "New rule"})
        self.assertEqual(report.topics, 2)

    def test_the_command_writes_where_it_is_told(self) -> None:
        #! @allow-local-import
        from core.assistant.knowledge_commands import KnowledgeCommandHandler
        #! @allow-local-import
        from core.knowledge.hypotheses import HypothesisTracker
        #! @allow-local-import
        from core.knowledge.review import KnowledgeReviewWorkflow

        output: list[str] = []
        handler = KnowledgeCommandHandler(
            KnowledgeReviewWorkflow(HypothesisTracker(self.graph)),
            output=lambda text, role=None: output.append(text),
            export_folder=self.root / "Exports",
        )
        self.assertTrue(handler.handle("/knowledge export", {}))
        self.assertIn("Exported 2 record(s)", output[-1])
        self.assertTrue(list((self.root / "Exports").glob("knowledge-*.md")))

    def test_everything_including_history_can_be_listed(self) -> None:
        self.assertEqual(len(self.graph.records.list_all(include_superseded=True)), 3)
        self.assertIn("# Iris memory", render_markdown([]))


class CorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self.tempdir.name) / "knowledge.db"))
        self.output: list[str] = []
        self.handler = CorrectionsCommandHandler(
            self.graph,
            last_request_id=lambda: "req-1",
            last_user_message=lambda: "What is the weather?",
            last_answer=lambda: "It is sunny in Boston.",
            output=lambda text, role=None: self.output.append(text),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_correction_keeps_the_why_with_what_it_corrects(self) -> None:
        self.assertTrue(self.handler.handle("/correct I asked about Austin, not Boston", {}))
        stored = self.graph.records.list_by_topic(TOPIC, kind=MemoryKind.OBSERVATION)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].content, "I asked about Austin, not Boston")
        self.assertEqual(stored[0].source_ref, "req-1")
        self.assertEqual(stored[0].data["asked"], "What is the weather?")
        self.assertIn("It is sunny", stored[0].data["answered"])

    def test_a_repeated_correction_is_pointed_at_becoming_a_principle(self) -> None:
        for _ in range(3):
            self.handler.handle("/correct Use Fahrenheit, not Celsius", {})
        self.assertIn("3th time", self.output[-1])
        self.assertIn("/knowledge observe iris/principles", self.output[-1])
        self.handler.handle("/corrections", {})
        self.assertIn("(x3)", self.output[-1])
        self.assertIn("Repeated enough to be principles", self.output[-1])

    def test_an_empty_correction_explains_itself(self) -> None:
        self.handler.handle("/correct", {})
        self.assertIn("Usage: /correct", self.output[-1])


if __name__ == "__main__":
    unittest.main()
