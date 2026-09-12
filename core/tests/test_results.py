# File: core/tests/test_results.py

"""The result contract (2.7): typed data with display hints, never pre-formatted prose."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from core.actions.implementations.fetch_web_page import FetchWebPageAction
from core.actions.models import ActionRequest
from core.knowledge.models import MemoryKind
from core.results.mcp import from_mcp, to_mcp
from core.results.models import Result, ResultError, ResultKind, Source, chart, code, diff, file, from_json_list, image, link, status, table, text, video
from core.results.render import render_markdown, to_detail, to_memory_record

SOURCE = Source("weather", "capability", "https://api.example.com/weather")


class ModelTests(unittest.TestCase):
    def test_every_kind_can_be_built_and_round_trips_through_json(self) -> None:
        results = [
            text("It is raining", source=SOURCE, title="Boston"),
            table(("city", "temp"), [("Boston", 48), ("Austin", 91)], source=SOURCE),
            image(mime_type="image/png", base64="AA==", source=SOURCE, alt="radar"),
            video("https://example.com/clip.mp4", source=SOURCE, mime_type="video/mp4"),
            file("E:\\\\Docs\\\\notes.txt", source=SOURCE, size_bytes=2048),
            code("print('hi')", source=SOURCE, language="python", path="a.py"),
            diff("--- a\n+++ b\n-x\n+y", source=SOURCE, path="a.py"),
            link("https://example.com", source=SOURCE, title="Example", author="A. Writer", date="2026-09-12"),
            chart("bar", [{"name": "temp", "values": [48, 91]}], labels=("Boston", "Austin"), source=SOURCE, units="F"),
            status("warning", "Two stations did not answer", source=SOURCE, details={"missing": 2}),
        ]
        self.assertEqual([item.kind for item in results], list(ResultKind))
        restored = from_json_list(json.loads(json.dumps([item.to_json() for item in results])))
        self.assertEqual([item.to_json() for item in restored], [item.to_json() for item in results])

    def test_every_result_carries_its_source_and_time(self) -> None:
        result = text("hello", source="lookup", ref="https://example.com/q")
        self.assertEqual(result.source, Source("lookup", "tool", "https://example.com/q"))
        self.assertTrue(result.created_at.endswith("+00:00"))

    def test_a_result_without_its_required_data_is_refused(self) -> None:
        with self.assertRaises(ResultError):
            Result(ResultKind.TABLE, SOURCE, {"columns": ["a"]})
        with self.assertRaises(ResultError):
            Result(ResultKind.IMAGE, SOURCE, {"mime_type": "image/png"})
        with self.assertRaises(ResultError):
            status("meh", "message", source=SOURCE)
        with self.assertRaises(ResultError):
            table(("a", "b"), [(1,)], source=SOURCE)

    def test_an_unknown_kind_is_skipped_on_the_way_in(self) -> None:
        self.assertEqual(from_json_list([{"kind": "hologram", "source": "x", "data": {}}]), ())


class RenderTests(unittest.TestCase):
    def test_a_table_renders_as_markdown_any_client_can_show(self) -> None:
        rendered = render_markdown(table(("city", "temp"), [("Boston", 48.0)], source=SOURCE, title="Temperatures"))
        self.assertIn("**Temperatures**", rendered)
        self.assertIn("| city | temp |", rendered)
        self.assertIn("| Boston | 48 |", rendered)

    def test_code_and_diffs_keep_their_fences(self) -> None:
        self.assertIn("```python\nprint('hi')\n```", render_markdown(code("print('hi')", source=SOURCE, language="python")))
        self.assertIn("```diff\n", render_markdown(diff("-x\n+y", source=SOURCE)))

    def test_a_link_carries_its_byline(self) -> None:
        rendered = render_markdown(link("https://example.com", source=SOURCE, title="Example", author="A. Writer", date="2026-09-12"))
        self.assertEqual(rendered, "[Example](https://example.com) — by A. Writer, dated 2026-09-12")

    def test_to_detail_keeps_the_typed_data_beside_the_rendering(self) -> None:
        """The window renders markdown today; the typed results ride along for a native renderer later."""
        detail = to_detail([status("ok", "All good", source=SOURCE)], title="Health")
        self.assertEqual(detail.type, "markdown")
        self.assertEqual(detail.title, "Health")
        self.assertIn("OK: All good", detail.content)
        self.assertEqual(detail.metadata["kinds"], ["status"])
        self.assertEqual(from_json_list(detail.metadata["results"])[0].data["message"], "All good")

    def test_a_result_can_be_saved_into_memory_as_it_is(self) -> None:
        record = to_memory_record(link("https://example.com", source=SOURCE, title="Example"), topic="research/example")
        self.assertEqual(record.kind, MemoryKind.OBSERVATION)
        self.assertEqual(record.source, "capability:weather")
        self.assertEqual(record.source_ref, "https://api.example.com/weather")
        self.assertEqual(record.data["result"]["kind"], "link")


class McpMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = Source("git:git_status", "mcp")

    def test_text_code_image_and_resources_come_across(self) -> None:
        blocks = [
            SimpleNamespace(type="text", text="On branch main"),
            SimpleNamespace(type="text", text="```python\nprint(1)\n```"),
            SimpleNamespace(type="image", data="AA==", mime_type="image/png"),
            SimpleNamespace(type="resource", resource=SimpleNamespace(uri="file:///tmp/a.txt", mime_type="text/plain", text="hello")),
            SimpleNamespace(type="resource", resource=SimpleNamespace(uri="file:///tmp/b.png", mime_type="image/png", blob="AA==")),
            SimpleNamespace(type="resource_link", uri="https://example.com/doc", name="Doc", description="A doc"),
        ]
        results = from_mcp(blocks, source=self.source)
        self.assertEqual(
            [item.kind for item in results],
            [ResultKind.TEXT, ResultKind.CODE, ResultKind.IMAGE, ResultKind.TEXT, ResultKind.IMAGE, ResultKind.LINK],
        )
        self.assertEqual(results[1].data["language"], "python")
        self.assertEqual(results[3].source.ref, "file:///tmp/a.txt")
        self.assertEqual(results[5].title, "Doc")

    def test_structured_content_alone_becomes_json_code(self) -> None:
        results = from_mcp([], source=self.source, structured={"branch": "main"})
        self.assertEqual(results[0].kind, ResultKind.CODE)
        self.assertEqual(results[0].data["language"], "json")

    def test_results_go_back_out_as_content_blocks(self) -> None:
        blocks = to_mcp([text("hello", source=SOURCE), image(mime_type="image/png", base64="AA==", source=SOURCE)])
        self.assertEqual([block.type for block in blocks], ["text", "image"])
        self.assertEqual(blocks[0].text, "hello")
        self.assertEqual(blocks[1].data, "AA==")

    def test_a_round_trip_keeps_the_text(self) -> None:
        original = code("x = 1", source=SOURCE, language="python")
        back = from_mcp(to_mcp([original]), source=self.source)
        self.assertEqual(back[0].kind, ResultKind.CODE)
        self.assertEqual(back[0].data["code"], "x = 1")


class _Page(SimpleNamespace):
    pass


class _Fetcher:
    def check_url(self, url: str) -> str:
        return url

    def fetch(self, url: str) -> Any:
        return _Page(
            title="Example Domain",
            source_line=f"Source: {url}",
            author="A. Writer",
            date="2026-09-12",
            text="This domain is for use in illustrative examples.",
            final_url=url,
            content_type="text/html",
        )


class ProducerTests(unittest.TestCase):
    def test_fetch_web_page_returns_a_link_and_the_text_not_only_prose(self) -> None:
        action = FetchWebPageAction(fetcher=_Fetcher())
        result = action.execute(ActionRequest(action="fetch_web_page", arguments={"url": "https://example.com/", "max_chars": 6000}), None)

        self.assertEqual(result.status, "success")
        self.assertEqual([item.kind for item in result.results], [ResultKind.LINK, ResultKind.TEXT])
        self.assertEqual(result.results[0].data["author"], "A. Writer")
        self.assertEqual(result.results[0].source.ref, "https://example.com/")
        self.assertIn("illustrative", result.results[1].data["text"])


if __name__ == "__main__":
    unittest.main()
