# File: core/tests/test_mcp_server.py

"""Iris as an MCP server (2.3): the memory surface, over the same spine."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.audit.stream import AuditCategory, AuditStream
from core.knowledge import KnowledgeGraph, KnowledgeRetriever, MemoryKind, MemoryStatus
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.review import KnowledgeReviewWorkflow
from core.mcp_server.server import READ_ONLY, build_server, build_tools
from core.mcp_server.tools import IrisMcpTools, McpToolError
from core.permissions.models import Decision, PermissionLevel
from core.permissions.policy import PermissionPolicy
from core.storage.sqlite_database import SQLiteDatabase
from core.tools.audit import ToolAuditor

TOPIC = "iris/design"


class _Service:
    def __init__(self, root: Path, permissions: Any = None) -> None:
        database = SQLiteDatabase(root / "knowledge.db")
        self.knowledge = KnowledgeGraph(database)
        self.knowledge_retriever = KnowledgeRetriever(database)
        self.knowledge_review = KnowledgeReviewWorkflow(HypothesisTracker(self.knowledge))
        self.audit_stream = AuditStream(root)
        self.tool_auditor = ToolAuditor(self.audit_stream)
        self.permissions = permissions


class McpToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.service = _Service(self.root)
        self.tools = build_tools(self.service, client="claude-code")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_what_is_observed_can_be_recalled(self) -> None:
        written = self.tools.observe(TOPIC, "The audit stream is one file with one schema")
        found = self.tools.recall("audit stream")

        self.assertEqual(found["records"][0]["id"], written["id"])
        self.assertEqual(found["records"][0]["topic"], TOPIC)

    def test_an_observation_is_closed_with_what_happened(self) -> None:
        observation = self.tools.observe(TOPIC, "Tried usearch for the vector index")
        outcome = self.tools.close_observation(observation["id"], "It removed the crash machinery", favourable=True)

        self.assertEqual(outcome["kind"], MemoryKind.OUTCOME.value)
        self.assertEqual(self.service.knowledge.outcome_for(observation["id"]).content, "It removed the crash machinery")
        self.assertEqual(self.service.knowledge.open_observations(TOPIC), [])

    def test_a_hypothesis_gathers_evidence_and_stops_short_of_accepted(self) -> None:
        """The line the memory phase drew holds for a caller Iris cannot see."""
        hypothesis = self.tools.hypothesize(TOPIC, "House rules keep new files from drifting")
        for index in range(5):
            evidence = self.tools.observe(TOPIC, f"File {index} passed the hook unchanged")
            assessment = self.tools.add_evidence(hypothesis["id"], evidence["id"], supports=True)

        self.assertEqual(assessment["status"], MemoryStatus.SUPPORTED.value)
        self.assertEqual(self.service.knowledge.records.get(hypothesis["id"]).status, MemoryStatus.SUPPORTED)
        waiting = self.tools.review_queue()
        self.assertEqual([item["id"] for item in waiting["awaiting_approval"]], [hypothesis["id"]])

    def test_a_hypothesis_cannot_be_used_as_its_own_evidence(self) -> None:
        hypothesis = self.tools.hypothesize(TOPIC, "Everything is fine")
        with self.assertRaises(McpToolError):
            self.tools.add_evidence(hypothesis["id"], hypothesis["id"], supports=True)

    def test_closing_something_that_is_not_an_observation_says_so(self) -> None:
        hypothesis = self.tools.hypothesize(TOPIC, "Everything is fine")
        with self.assertRaises(McpToolError) as caught:
            self.tools.close_observation(hypothesis["id"], "done")
        self.assertIn("hypothesis", str(caught.exception))

    def test_a_record_needs_a_topic_and_content(self) -> None:
        with self.assertRaises(McpToolError):
            self.tools.observe("", "content")
        with self.assertRaises(McpToolError):
            self.tools.observe(TOPIC, "   ")

    def test_explain_says_where_a_memory_came_from(self) -> None:
        observation = self.tools.observe(TOPIC, "Vector writes moved out of process")
        self.assertIn("Vector writes", self.tools.explain(observation["id"])["explanation"])

    def test_every_call_lands_in_the_audit_trail(self) -> None:
        self.tools.observe(TOPIC, "Something happened")
        self.tools.recall("something")

        events = self.service.audit_stream.read(category=AuditCategory.TOOL)
        self.assertEqual([item.event for item in events], ["observe", "recall"])
        self.assertEqual(events[0].source, "mcp:claude-code")
        self.assertEqual(events[0].subject, "mcp")


class McpPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _tools(self, policy: PermissionPolicy) -> IrisMcpTools:
        return build_tools(_Service(self.root, permissions=policy))

    def test_a_denied_level_stops_the_caller(self) -> None:
        tools = self._tools(PermissionPolicy(modes={PermissionLevel.WRITE: Decision.DENY}))
        with self.assertRaises(McpToolError) as caught:
            tools.observe(TOPIC, "Something happened")
        self.assertIn("Not allowed", str(caught.exception))

    def test_a_level_that_asks_a_person_cannot_be_answered_over_mcp(self) -> None:
        """There is nobody at the other end of an MCP call to confirm anything."""
        tools = self._tools(PermissionPolicy(modes={PermissionLevel.WRITE: Decision.CONFIRM}))
        with self.assertRaises(McpToolError) as caught:
            tools.observe(TOPIC, "Something happened")
        self.assertIn("confirm", str(caught.exception))

    def test_reading_is_untouched_by_a_write_rule(self) -> None:
        tools = self._tools(PermissionPolicy(modes={PermissionLevel.WRITE: Decision.DENY}))
        self.assertEqual(tools.recall("anything")["records"], [])

    def test_a_refusal_is_audited_as_a_permission_decision(self) -> None:
        service = _Service(self.root)
        service.permissions = PermissionPolicy(
            modes={PermissionLevel.WRITE: Decision.DENY}, audit=service.audit_stream
        )
        tools = build_tools(service)
        with self.assertRaises(McpToolError):
            tools.observe(TOPIC, "Something happened")
        events = service.audit_stream.read(category=AuditCategory.PERMISSION)
        self.assertEqual(events[0].event, "observe")
        self.assertEqual(events[0].source, "mcp:mcp")


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = _Service(Path(self.tempdir.name))
        self.server = build_server(self.service, client="claude-code")
        self.tools = asyncio.run(self.server.list_tools())

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_the_server_offers_the_memory_surface(self) -> None:
        names = {tool.name for tool in self.tools}
        self.assertEqual(
            names,
            {"recall", "observe", "close_observation", "hypothesize", "add_evidence", "assess", "review_queue", "explain"},
        )

    def test_the_read_only_tools_say_so_to_the_client(self) -> None:
        for tool in self.tools:
            annotations = tool.annotations
            self.assertEqual(bool(annotations and annotations.read_only_hint), tool.name in READ_ONLY)

    def test_arguments_are_declared_so_a_client_can_call_them(self) -> None:
        recall = next(tool for tool in self.tools if tool.name == "recall")
        self.assertIn("text", recall.input_schema["properties"])
        self.assertEqual(recall.input_schema.get("required"), ["text"])

    def test_a_call_through_the_server_reaches_the_memory(self) -> None:
        asyncio.run(self.server.call_tool("observe", {"topic": TOPIC, "content": "Called over MCP"}))
        found = self.service.knowledge.records.list_by_topic(TOPIC, kind=MemoryKind.OBSERVATION)
        self.assertEqual(found[0].content, "Called over MCP")


class LiveStdioTests(unittest.TestCase):
    """The transport a client actually uses, driven by a real client."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "Data" / "Memory").mkdir(parents=True)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "assistant_name": "Iris",
                    "memory_path": str(self.root / "Data" / "Memory"),
                    "model": "qwen3:8b",
                    "llm_server": "http://127.0.0.1:11434",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    async def _round_trip(self) -> tuple[set[str], str | None]:
        #! @allow-local-import
        from mcp import ClientSession, StdioServerParameters
        #! @allow-local-import
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=sys.executable,
            args=["mcp_server.py", "--config", str(self.config_path), "--client", "test"],
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                await session.call_tool("observe", {"topic": TOPIC, "content": "Reached Iris over stdio"})
                found = await session.call_tool("recall", {"text": "stdio"})
                records = (found.structured_content or {}).get("records") or []
                return {tool.name for tool in listing.tools}, records[0]["content"] if records else None

    def test_a_client_can_start_the_server_and_use_the_memory(self) -> None:
        names, recalled = asyncio.run(asyncio.wait_for(self._round_trip(), timeout=120))

        self.assertIn("recall", names)
        self.assertEqual(recalled, "Reached Iris over stdio")


if __name__ == "__main__":
    unittest.main()
