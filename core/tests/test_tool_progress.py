# File: core/tests/test_tool_progress.py

"""Per-tool progress during a request (6): what is running, and how it ended."""

from __future__ import annotations

import unittest

from core.assistant.tool_progress import describe_tool_event, is_tool_progress


class DescribeTests(unittest.TestCase):
    def test_start_names_the_tool_and_its_target(self) -> None:
        self.assertEqual(describe_tool_event({"phase": "start", "name": "web_search", "arguments": {"query": "platypus venom"}}), "Running web_search on platypus venom…")
        self.assertEqual(describe_tool_event({"phase": "start", "name": "system_overview", "arguments": {}}), "Running system_overview…")
        long_target = "x" * 80
        self.assertTrue(describe_tool_event({"phase": "start", "name": "repo_search", "arguments": {"pattern": long_target}}).endswith("...…"))

    def test_end_reports_timing_and_outcome(self) -> None:
        self.assertEqual(describe_tool_event({"phase": "end", "name": "web_search", "status": "success", "ms": 1234.5}), "Finished web_search in 1.2 s: ok")
        self.assertEqual(describe_tool_event({"phase": "end", "name": "update_config", "status": "pending_confirmation", "ms": 50}), "Finished update_config in 0.1 s: waiting for approval")
        self.assertEqual(describe_tool_event({"phase": "end", "name": "excel_read", "status": "failed", "error": "no workbook"}), "Finished excel_read: failed (no workbook)")

    def test_only_progress_lines_are_recognised(self) -> None:
        self.assertTrue(is_tool_progress("Running x…"))
        self.assertTrue(is_tool_progress("Finished x: ok"))
        self.assertFalse(is_tool_progress("Scanning root: E:\\Docs"))


if __name__ == "__main__":
    unittest.main()
