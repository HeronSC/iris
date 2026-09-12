# File: core/tests/test_learning_loop.py

"""Section 2.2 closed out: principles from repeated corrections, into the prompt, switchable; observable outcomes; gaps."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.memory_tools import RecordGapAction
from core.actions.models import ActionRequest
from core.assistant.changes_command import ChangesCommandHandler
from core.assistant.corrections_command import CorrectionsCommandHandler
from core.assistant.knowledge_commands import KnowledgeCommandHandler
from core.assistant.learning import LearningLoop
from core.assistant.principles_command import PrinciplesCommandHandler
from core.knowledge.graph import KnowledgeGraph
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.models import MemoryKind, MemoryStatus
from core.knowledge.principles import GAP_TOPIC, TOPIC, PrincipleService, principles_block
from core.knowledge.review import KnowledgeReviewWorkflow
from core.storage.sqlite_database import SQLiteDatabase


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self.tempdir.name) / "knowledge.db"))
        self.principles = PrincipleService(self.graph.records)
        self.lines: list[str] = []
        self.sink = lambda text, role=None: self.lines.append(text)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, handler, command: str) -> str:
        del self.lines[:]
        self.assertTrue(handler.handle(command, {}))
        return "\n".join(self.lines)


class PrincipleTests(_Base):
    def test_add_list_switch_and_edit(self) -> None:
        handler = PrinciplesCommandHandler(self.principles, output=self.sink)
        self.assertIn("No principles yet", self._run(handler, "/principles"))
        self.assertIn("Added principle 1: Always quote AL object names", self._run(handler, "/principles add Always quote AL object names"))
        self.assertIn("Already a principle (1)", self._run(handler, "/principles add always quote al object names"))
        self.assertIn("Added principle 2", self._run(handler, "/principles add Prefer events over modifying base code"))
        self.assertEqual(self.principles.active_lines(), ["Always quote AL object names", "Prefer events over modifying base code"])
        self.assertIn("Principle 1 is now off", self._run(handler, "/principles off 1"))
        self.assertEqual(self.principles.active_lines(), ["Prefer events over modifying base code"])
        listing = self._run(handler, "/principles")
        self.assertIn("2 principles, 1 on", listing)
        self.assertIn("1. [off] Always quote AL object names", listing)
        self.assertIn("Principle 1 is now on", self._run(handler, "/principles on 1"))
        edited = self._run(handler, "/principles edit 2 Prefer event subscribers over changing base objects")
        self.assertIn("now reads: Prefer event subscribers", edited)
        self.assertEqual(len(self.principles.all()), 2)
        history = [record for record in self.graph.records.list_by_topic(TOPIC, include_superseded=True) if record.status is MemoryStatus.SUPERSEDED]
        self.assertEqual(len(history), 1)
        self.assertIn("No principle 9", self._run(handler, "/principles off 9"))
        self.assertTrue(self._run(handler, "/principles bogus").startswith("Usage"))
        block = principles_block(self.principles.active_lines())
        self.assertTrue(block.startswith("Working principles the user has set"))
        self.assertIn("- Always quote AL object names", block)
        self.assertEqual(principles_block([]), "")

    def test_the_third_repeat_of_a_correction_becomes_a_principle(self) -> None:
        handler = CorrectionsCommandHandler(self.graph, last_request_id=lambda: "r1", last_user_message=lambda: "make a page", last_answer=lambda: "here", output=self.sink, principles=self.principles)
        self._run(handler, "/correct Use the ELEP prefix on every object")
        self._run(handler, "/correct use the ELEP prefix on every object.")
        self.assertEqual(self.principles.all(), [])
        third = self._run(handler, "/correct Use the ELEP prefix on every object")
        self.assertIn("now principle 1 and goes into every prompt", third)
        self.assertEqual(self.principles.active_lines(), ["Use the ELEP prefix on every object"])
        self.assertEqual(self.principles.all()[0].record.data["origin"], "3 corrections")
        fourth = self._run(handler, "/correct Use the ELEP prefix on every object")
        self.assertIn("principle 1 already covers it", fourth)


class OutcomeTests(_Base):
    def test_an_undo_is_noted_as_a_correction(self) -> None:
        loop = LearningLoop(self.graph, request_id=lambda: "r9")
        report = SimpleNamespace(ok=True, summary="Put back config.json", change=SimpleNamespace(action="update_config", files=["E:\\AI\\iris\\core\\config.json"]))
        ledger = SimpleNamespace(undo=lambda change_id: report, recent=lambda limit=10: [])
        handler = ChangesCommandHandler(ledger, output=self.sink, on_undo=loop.note_undo)
        self.assertTrue(handler.handle("/undo", {}))
        records = self.graph.records.list_by_topic("iris/corrections")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].content, "The user undid Iris's change: update_config on config.json")
        self.assertEqual(records[0].source, "user:undo")
        self.assertEqual(records[0].source_ref, "r9")

    def test_a_compile_failure_after_an_edit_is_noted_and_a_clean_one_is_not(self) -> None:
        loop = LearningLoop(self.graph)
        self.assertIsNone(loop.note_tool_event({"phase": "end", "name": "edit_file", "status": "success", "target": "E:\\x\\Sync.al"}))
        noted = loop.note_tool_event({"phase": "end", "name": "al_compile", "status": "success", "summary": "Mammoth Projects failed to compile: 2 errors, 0 warnings in 4 s"})
        self.assertIsNotNone(noted)
        self.assertIn("failed to compile", noted.content)
        self.assertEqual(noted.source, "iris:compile")
        self.assertIsNone(loop.last_edit)
        self.assertIsNone(loop.note_tool_event({"phase": "end", "name": "al_compile", "status": "success", "summary": "Mammoth Projects failed to compile: 2 errors"}))
        loop.note_tool_event({"phase": "end", "name": "write_file", "status": "success", "target": "E:\\x\\New.al"})
        self.assertIsNone(loop.note_tool_event({"phase": "end", "name": "al_compile", "status": "success", "summary": "Mammoth Projects compiled: 0 errors, 0 warnings in 4 s"}))
        self.assertIsNone(loop.last_edit)
        self.assertEqual(len(self.graph.records.list_by_topic("iris/corrections")), 1)


class GapTests(_Base):
    def test_a_gap_is_recorded_once_listed_and_closable(self) -> None:
        action = RecordGapAction()
        context = SimpleNamespace(knowledge=self.graph)
        validation = action.validate(ActionRequest(action="record_gap", arguments={"question": "Which tenant does the sandbox use?", "context": "launch.json has no tenant"}), context)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="record_gap", arguments=validation.resolved_arguments), context)
        self.assertEqual(result.status, "success")
        self.assertIn("Recorded as an open question", result.message)
        again = action.execute(ActionRequest(action="record_gap", arguments={"question": "which tenant does the sandbox use", "context": ""}), context)
        self.assertEqual(again.resolved_target, result.resolved_target)
        self.assertEqual(len(self.graph.records.list_by_topic(GAP_TOPIC)), 1)
        knowledge = KnowledgeCommandHandler(KnowledgeReviewWorkflow(HypothesisTracker(self.graph)), output=self.sink)
        listing = self._run(knowledge, "/knowledge gaps")
        self.assertIn("1 open question", listing)
        self.assertIn("Which tenant does the sandbox use?", listing)
        self._run(knowledge, f"/knowledge outcome {result.resolved_target[:8]} It is the Sandbox tenant b681")
        self.assertIn("No open questions", self._run(knowledge, "/knowledge gaps"))
        self.assertEqual(action.definition.permission.value, "write")
        missing = action.validate(ActionRequest(action="record_gap", arguments={"question": "x"}), SimpleNamespace())
        self.assertFalse(missing.ok)


if __name__ == "__main__":
    unittest.main()
