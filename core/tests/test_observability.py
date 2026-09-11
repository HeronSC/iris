# File: core/tests/test_observability.py

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

import structlog

from core.assistant.why_command import WhyCommandHandler
from core.llm.metrics import RequestMetric, RequestMetricsStore
from core.observability import (
    bind_request,
    clear_request,
    configure_logging,
    current_request_id,
    log_dir_for,
    new_request_id,
    request_scope,
)
from core.observability.logging_setup import read_log_entries
from core.storage.sqlite_database import SQLiteDatabase


class RequestContextTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_request()

    def test_bind_and_clear(self) -> None:
        self.assertIsNone(current_request_id())
        rid = bind_request()
        self.assertEqual(len(rid), 12)
        self.assertEqual(current_request_id(), rid)
        clear_request()
        self.assertIsNone(current_request_id())

    def test_scope_restores(self) -> None:
        with request_scope("abc") as rid:
            self.assertEqual(rid, "abc")
            self.assertEqual(current_request_id(), "abc")
        self.assertIsNone(current_request_id())
        self.assertNotEqual(new_request_id(), new_request_id())


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_file = configure_logging(Path(self._tmp.name), level=logging.DEBUG)

    def tearDown(self) -> None:
        clear_request()
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_iris_logging_handler", False):
                root.removeHandler(handler)
                handler.close()
        self._tmp.cleanup()

    def test_structlog_and_stdlib_lines_share_the_file_and_the_request_id(self) -> None:
        with request_scope("req-1"):
            structlog.get_logger("core.test").info("turn", route="intent", elapsed_ms=12.5)
            logging.getLogger("mcp.client").warning("server slow")
        logging.getLogger("other").info("outside any request")

        for handler in logging.getLogger().handlers:
            handler.flush()
        lines = [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        by_event = {entry["event"]: entry for entry in lines}
        self.assertEqual(by_event["turn"]["request_id"], "req-1")
        self.assertEqual(by_event["turn"]["route"], "intent")
        self.assertEqual(by_event["turn"]["level"], "info")
        self.assertIn("timestamp", by_event["turn"])
        self.assertEqual(by_event["server slow"]["request_id"], "req-1")
        self.assertEqual(by_event["server slow"]["logger"], "mcp.client")
        self.assertNotIn("request_id", by_event["outside any request"])

        only = read_log_entries(self.log_file, request_id="req-1")
        self.assertEqual([entry["event"] for entry in only], ["turn", "server slow"])

    def test_configure_twice_does_not_duplicate_handlers(self) -> None:
        before = len(logging.getLogger().handlers)
        configure_logging(Path(self._tmp.name))
        self.assertEqual(len(logging.getLogger().handlers), before)

    def test_log_dir_defaults_beside_the_data_folder(self) -> None:
        self.assertEqual(log_dir_for({"memory_path": "E:\\AI\\Iris\\Data\\Memory"}), Path("E:\\AI\\Iris\\Data\\logs"))
        self.assertEqual(log_dir_for({"log_path": "D:\\logs"}), Path("D:\\logs"))


class WhyCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.log_file = root / "iris.jsonl"
        self.trace_file = root / "request_trace.jsonl"
        self.metrics = RequestMetricsStore(SQLiteDatabase(root / "metrics.db"))
        self.output: list[str] = []

        self.log_file.write_text(
            "\n".join(
                json.dumps(entry)
                for entry in [
                    {"event": "turn", "level": "info", "request_id": "aaa", "route": "coordinator", "status": "complete", "elapsed_ms": 1234.5, "user_message": "what's the weather"},
                    {"event": "Model routes: qwen2.5vl:7b is not pulled", "level": "warning", "request_id": "aaa", "logger": "core.application.service"},
                    {"event": "turn", "level": "info", "request_id": "bbb", "route": "intent", "status": "complete", "elapsed_ms": 900.0, "user_message": "launch code"},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.trace_file.write_text(
            json.dumps({"request_id": "aaa", "deterministic_parser_result": {"intent": "weather"}, "selected_tool": "weather", "tool_arguments": {"location": "Boston"}, "tool_result": {"status": "success"}, "final_response": "Sunny, 72F.", "model": "qwen3:8b"})
            + "\n",
            encoding="utf-8",
        )
        self.metrics.record(RequestMetric(task="decision", requested_model="qwen3:8b", model="qwen3:8b", outcome="ok", prompt_tokens=300, completion_tokens=20, wall_ms=800, request_id="aaa"))
        self.metrics.record(RequestMetric(task="chat", requested_model="qwen3:8b", model="qwen3:8b", outcome="ok", prompt_tokens=500, completion_tokens=40, wall_ms=1500, request_id="aaa"))
        self.metrics.record(RequestMetric(task="intent", requested_model="qwen3:8b", model="qwen3:8b", outcome="ok", prompt_tokens=200, completion_tokens=10, wall_ms=700, tool_calls=("launch_application",), request_id="bbb"))

        class Audit:
            def read_recent(self, limit: int = 20):
                return [
                    {"request_id": "bbb", "action": "launch_application", "tool": "launch_application", "status": "success", "message": "Launched Visual Studio Code", "resolved_target": "C:\\Code.exe"},
                    {"request_id": "zzz", "action": "open_url", "status": "success", "message": "URL opened"},
                ]

        self.handler = WhyCommandHandler(
            log_file=self.log_file,
            trace_file=self.trace_file,
            metrics=self.metrics,
            action_audit=Audit(),
            last_request_id=lambda: "bbb",
            output=lambda text, role=None: self.output.append(text),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_explains_the_last_request_by_default(self) -> None:
        self.assertFalse(self.handler.handle("/whyever", {}))
        self.assertTrue(self.handler.handle("/why", {}))
        text = self.output[-1]
        self.assertIn("Request bbb", text)
        self.assertIn("You asked: launch code", text)
        self.assertIn("Route: intent -> complete in 900 ms", text)
        self.assertIn("intent -> qwen3:8b: 200+10 tokens, 700 ms, called launch_application", text)
        self.assertIn("launch_application on C:\\Code.exe: success", text)
        self.assertNotIn("open_url", text)

    def test_explains_a_coordinator_turn_with_its_trace(self) -> None:
        self.assertTrue(self.handler.handle("/why aaa", {}))
        text = self.output[-1]
        self.assertIn("You asked: what's the weather", text)
        self.assertIn("Parsed intent: weather", text)
        self.assertIn('Capability: weather {"location": "Boston"} -> success', text)
        self.assertIn("Answer: Sunny, 72F.", text)
        self.assertIn("decision -> qwen3:8b: 300+20 tokens", text)
        self.assertIn("chat -> qwen3:8b: 500+40 tokens", text)
        self.assertIn("Warnings:", text)
        self.assertIn("not pulled", text)

    def test_unknown_request(self) -> None:
        self.assertTrue(self.handler.handle("/why nope", {}))
        self.assertIn("Nothing recorded for request nope", self.output[-1])
        empty = WhyCommandHandler(log_file=None, trace_file=None, metrics=None, action_audit=None, last_request_id=lambda: None, output=lambda t, r=None: self.output.append(t))
        empty.handle("/why", {})
        self.assertIn("No request to explain yet", self.output[-1])


if __name__ == "__main__":
    unittest.main()
