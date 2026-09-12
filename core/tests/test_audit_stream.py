# File: core/tests/test_audit_stream.py

"""One audit trail, one schema (2.8), and nothing secret in it (10)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.actions.audit import ActionAuditLogger
from core.audit.logger import AuditLogger
from core.audit.redaction import REDACTED, redact
from core.audit.stream import AUDIT_FILE_NAME, AuditCategory, AuditEvent, AuditStream
from core.observability.request_context import request_scope
from core.tools.audit import ToolAuditor
from core.tools.models import PermissionLevel, ToolDefinition, ToolKind
from core.tools.registry import ToolRegistry


class AuditStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.stream = AuditStream(self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _lines(self) -> list[dict]:
        return [
            json.loads(line)
            for line in (self.root / AUDIT_FILE_NAME).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_memory_and_actions_share_one_file_and_one_schema(self) -> None:
        """The point of the unification: two writers, one file, same field names."""
        AuditLogger(self.root).log({"action": "approved", "subject": "hypothesis", "actor": "henry"})
        ActionAuditLogger(self.root).log(
            {"action": "greet", "tool": "greet_loudly", "status": "success", "message": "hi"}
        )

        lines = self._lines()
        self.assertEqual([line["category"] for line in lines], ["memory", "action"])
        for line in lines:
            self.assertEqual(sorted(set(line) & {"created_at", "category", "event"}), ["category", "created_at", "event"])
        self.assertEqual(lines[0]["event"], "approved")
        self.assertEqual(lines[1]["event"], "greet_loudly")
        self.assertEqual(lines[1]["subject"], "greet")

    def test_each_reader_gets_its_own_entries_back(self) -> None:
        memory = AuditLogger(self.root)
        actions = ActionAuditLogger(self.root)
        memory.log({"action": "declined", "actor": "henry", "detail": "Too risky.", "hypothesis_id": "abc"})
        actions.log({"action": "open_url", "status": "success", "resolved_target": "https://example.com"})

        entries = memory.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "declined")
        self.assertEqual(entries[0]["actor"], "henry")
        self.assertEqual(entries[0]["detail"], "Too risky.")
        self.assertEqual(entries[0]["hypothesis_id"], "abc")

        recent = actions.read_recent(limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["action"], "open_url")
        self.assertEqual(recent[0]["resolved_target"], "https://example.com")

    def test_history_written_before_the_change_is_still_read(self) -> None:
        """The old files are not migrated, so they have to stay readable."""
        (self.root / "actions.jsonl").write_text(
            json.dumps({"action": "open_file", "status": "success"}) + "\n", encoding="utf-8"
        )
        logger = ActionAuditLogger(self.root)
        logger.log({"action": "open_url", "status": "success"})

        entries = logger.read_recent(limit=10)
        self.assertEqual([entry.get("action") for entry in entries], ["open_file", "open_url"])

    def test_the_turn_id_ties_the_entry_to_the_request(self) -> None:
        with request_scope("req-1234"):
            self.stream.record(AuditCategory.TOOL, "weather", status="success")
        self.assertEqual(self.stream.read(request_id="req-1234")[0].event, "weather")
        self.assertEqual(self.stream.read(request_id="other"), [])

    def test_reading_by_category_leaves_the_rest_alone(self) -> None:
        self.stream.record(AuditCategory.TOOL, "weather", status="success")
        self.stream.record(AuditCategory.PERMISSION, "open_file", status="denied")
        self.assertEqual([item.event for item in self.stream.read(category=AuditCategory.PERMISSION)], ["open_file"])
        self.assertEqual(len(self.stream.read()), 2)

    def test_a_line_that_will_not_parse_is_skipped(self) -> None:
        self.stream.record(AuditCategory.TOOL, "weather", status="success")
        with self.stream.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        self.stream.record(AuditCategory.TOOL, "news", status="success")
        self.assertEqual([item.event for item in self.stream.read()], ["weather", "news"])

    def test_data_cannot_shadow_a_canonical_field(self) -> None:
        event = AuditEvent(category=AuditCategory.TOOL, event="weather", status="success", data={"status": "nonsense"})
        self.assertEqual(event.flatten()["status"], "success")


class RedactionTests(unittest.TestCase):
    """Section 10: never log secrets or credential values."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.stream = AuditStream(self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_credential_never_reaches_the_file(self) -> None:
        self.stream.record(
            AuditCategory.TOOL,
            "unifi",
            status="success",
            data={"api_key": "live-key-1", "host": "10.0.0.1", "nested": {"Client Secret": "s3cret"}},
        )
        raw = self.stream.path.read_text(encoding="utf-8")
        self.assertNotIn("live-key-1", raw)
        self.assertNotIn("s3cret", raw)
        self.assertIn("10.0.0.1", raw)
        stored = self.stream.read()[0]
        self.assertEqual(stored.data["api_key"], REDACTED)
        self.assertEqual(stored.data["nested"]["Client Secret"], REDACTED)

    def test_a_secret_embedded_in_text_is_rewritten(self) -> None:
        self.stream.record(
            AuditCategory.ACTION,
            "fetch_web_page",
            message="fetching https://henry:hunter2@example.com/feed",
            data={"headers": "Authorization: Bearer abcdef123456"},
        )
        raw = self.stream.path.read_text(encoding="utf-8")
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("abcdef123456", raw)

    def test_a_token_count_is_not_a_token(self) -> None:
        self.assertEqual(redact({"prompt_tokens": 41})["prompt_tokens"], 41)


class ToolAuditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.stream = AuditStream(self.root)
        self.registry = ToolRegistry()
        self.registry.register(
            ToolDefinition(name="weather", description="Weather.", kind=ToolKind.CAPABILITY, permission=PermissionLevel.READ)
        )
        self.registry.register(
            ToolDefinition(name="open_file", description="Open a file.", kind=ToolKind.ACTION, permission=PermissionLevel.READ)
        )
        self.auditor = ToolAuditor(self.stream, self.registry)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_a_capability_call_is_recorded(self) -> None:
        """Command tools and knowledge providers were the hole in the trail."""
        self.auditor.record("weather", {"location": "Boston"}, {"status": "success", "message": "48F and raining"})

        event = self.stream.read(category=AuditCategory.TOOL)[0]
        self.assertEqual(event.event, "weather")
        self.assertEqual(event.subject, "capability")
        self.assertEqual(event.status, "success")
        self.assertEqual(event.data["arguments"], {"location": "Boston"})
        self.assertEqual(event.data["permission"], "read")

    def test_an_action_is_left_to_the_executor(self) -> None:
        """Two lines for one call would make counting invocations wrong."""
        self.assertIsNone(self.auditor.record("open_file", {"file_id": "1"}, {"status": "success"}))
        self.assertEqual(self.stream.read(category=AuditCategory.TOOL), [])

    def test_a_long_answer_is_trimmed_to_its_first_line(self) -> None:
        self.auditor.record("weather", {}, {"status": "success", "message": "48F\nand a long tail of prose"})
        self.assertEqual(self.stream.read(category=AuditCategory.TOOL)[0].message, "48F")


if __name__ == "__main__":
    unittest.main()
