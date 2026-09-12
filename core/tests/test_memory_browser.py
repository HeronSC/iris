# File: core/tests/test_memory_browser.py

"""The memory browser (2.1, 6): records as a table with show, why, supersede and forget on every row."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.actions.implementations.memory_tools import MemoryBrowseAction
from core.actions.models import ActionRequest
from core.knowledge.graph import KnowledgeGraph
from core.knowledge.models import MemoryKind, MemoryRecord
from core.results.html import compose_url, parse_iris_url, render_result, run_url
from core.results.models import Source, table
from core.storage.sqlite_database import SQLiteDatabase


class LinkTests(unittest.TestCase):
    def test_run_and_compose_links_round_trip(self) -> None:
        self.assertEqual(parse_iris_url(run_url("/knowledge show 3f2a")), ("run", "/knowledge show 3f2a"))
        self.assertEqual(parse_iris_url(compose_url("/knowledge forget 3f2a ")), ("compose", "/knowledge forget 3f2a "))
        self.assertEqual(parse_iris_url("iris://confirm"), ("run", "/confirm"))
        self.assertEqual(parse_iris_url("iris://cancel"), ("run", "/cancel"))
        self.assertEqual(parse_iris_url("iris://open?path=E%3A%5CDocs")[0], "open")
        self.assertIsNone(parse_iris_url("https://example.com"))
        self.assertIsNone(parse_iris_url("iris://unknown?x=1"))

    def test_tables_render_row_actions_as_links(self) -> None:
        result = table(("id", "text"), [("3f2a", "one"), ("9b1c", "two")], source=Source("memory_browse", "memory", "all"))
        result.data["row_actions"] = [[{"label": "show", "href": run_url("/knowledge show 3f2a")}], []]
        rendered = render_result(result)
        self.assertIn('<td class="actions"><a href="iris://run?cmd=%2Fknowledge%20show%203f2a">show</a></td>', rendered)
        self.assertEqual(rendered.count('<td class="actions">'), 2)
        plain = render_result(table(("id",), [("x",)], source=Source("t", "tool")))
        self.assertNotIn("actions", plain)


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self.tempdir.name) / "knowledge.db"))
        self.graph.records.add_many(
            [
                MemoryRecord(kind=MemoryKind.FACT, topic="bc/dispatch", content="The pool page is 99510", source="user", scope="project:mammoth"),
                MemoryRecord(kind=MemoryKind.OBSERVATION, topic="trading/candidates", content="Saw a breakout", source="bot"),
            ]
        )
        self.execution = SimpleNamespace(knowledge=self.graph)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_browse_lists_records_with_actions_and_filters(self) -> None:
        action = MemoryBrowseAction()
        validation = action.validate(ActionRequest(action="memory_browse", arguments={}), self.execution)
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="memory_browse", arguments=validation.resolved_arguments), self.execution)
        self.assertEqual(result.status, "success")
        self.assertIn("2 records, newest first", result.message)
        rows = result.results[0].data["rows"]
        self.assertEqual([row[1] for row in rows], ["observation", "fact"])
        self.assertEqual(rows[1][3], "project:mammoth")
        actions = result.results[0].data["row_actions"]
        self.assertEqual([item["label"] for item in actions[0]], ["show", "why", "supersede", "forget"])
        self.assertTrue(actions[0][3]["href"].startswith("iris://compose?text=%2Fknowledge%20forget%20"))
        topic = action.execute(ActionRequest(action="memory_browse", arguments={"topic": "bc/dispatch", "limit": 20}), self.execution)
        self.assertIn("1 record in bc/dispatch", topic.message)
        kind = action.execute(ActionRequest(action="memory_browse", arguments={"kind": "fact", "limit": 20}), self.execution)
        self.assertEqual(len(kind.results[0].data["rows"]), 1)
        empty = action.execute(ActionRequest(action="memory_browse", arguments={"topic": "nothing", "limit": 20}), self.execution)
        self.assertIn("No records in nothing", empty.message)
        bad = action.validate(ActionRequest(action="memory_browse", arguments={"kind": "rumour"}), self.execution)
        self.assertFalse(bad.ok)
        missing = action.validate(ActionRequest(action="memory_browse", arguments={}), SimpleNamespace())
        self.assertFalse(missing.ok)


if __name__ == "__main__":
    unittest.main()
