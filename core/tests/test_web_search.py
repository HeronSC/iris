# File: core/tests/test_web_search.py

"""General web search (5.1) through a local SearXNG: keyless, four categories, cited, cached."""

from __future__ import annotations

import json
import unittest

import httpx

from core.actions.implementations.web_search import WebSearchAction
from core.actions.models import ActionRequest
from core.results.models import ResultKind
from core.web.search import SearchClient, SearchUnavailable

PAYLOAD = {
    "query": "platypus venom",
    "results": [
        {"url": "https://example.org/platypus", "title": "Platypus venom", "content": "Males have a spur.", "engines": ["duckduckgo", "brave"], "score": 4.0, "publishedDate": "2026-01-02T00:00:00"},
        {"url": "https://example.com/dup", "title": "Dup", "content": "one", "engine": "bing", "score": 1.0},
        {"url": "https://example.com/dup", "title": "Dup again", "content": "two", "engine": "google", "score": 0.5},
        {"url": "https://example.net/low", "title": "Low", "content": "", "engines": ["qwant"], "score": 2.0},
    ],
    "suggestions": ["platypus spur", "monotreme venom"],
    "unresponsive_engines": [["startpage", "timeout"]],
}

IMAGE_PAYLOAD = {
    "results": [
        {"url": "https://example.org/photo", "title": "A platypus", "img_src": "https://cdn.example.org/platypus.png", "thumbnail_src": "https://cdn.example.org/t.png", "engine": "bing images", "score": 1.0},
    ]
}


class RecordingTransport(httpx.MockTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, text="OK")
        if request.url.params.get("format") != "json":
            return httpx.Response(403, text="forbidden")
        if request.url.params.get("categories") == "images":
            return httpx.Response(200, json=IMAGE_PAYLOAD)
        return httpx.Response(200, json=PAYLOAD)


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = RecordingTransport()
        self.client = SearchClient(transport=self.transport)

    def test_a_search_sends_the_right_query_and_ranks_deduplicated_hits(self) -> None:
        response = self.client.search("platypus  venom", category="general", time_range="year")
        request = self.transport.requests[-1]
        self.assertEqual(request.url.path, "/search")
        self.assertEqual(request.url.params["q"], "platypus venom")
        self.assertEqual(request.url.params["categories"], "general")
        self.assertEqual(request.url.params["time_range"], "year")
        self.assertEqual([hit.url for hit in response.hits], ["https://example.org/platypus", "https://example.net/low", "https://example.com/dup"])
        self.assertEqual(response.hits[0].engines, ("duckduckgo", "brave"))
        self.assertEqual(response.hits[0].published, "2026-01-02")
        self.assertEqual(response.suggestions, ("platypus spur", "monotreme venom"))
        self.assertEqual(response.unresponsive, ("startpage (timeout)",))
        self.assertIn("SearXNG general search", response.source_line)

    def test_the_same_search_is_served_from_cache_and_trimmed(self) -> None:
        first = self.client.search("platypus venom", max_results=2)
        second = self.client.search("Platypus venom", max_results=1)
        self.assertEqual(len(self.transport.requests), 1)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(len(first.hits), 2)
        self.assertEqual(len(second.hits), 1)

    def test_bad_arguments_are_refused_before_any_request(self) -> None:
        with self.assertRaises(ValueError):
            self.client.search("   ")
        with self.assertRaises(ValueError):
            self.client.search("x", category="maps")
        with self.assertRaises(ValueError):
            self.client.search("x", time_range="decade")
        self.assertEqual(self.transport.requests, [])

    def test_a_stopped_container_says_how_to_start_it(self) -> None:
        client = SearchClient(transport=httpx.MockTransport(_down))
        with self.assertRaises(SearchUnavailable) as caught:
            client.search("anything")
        self.assertIn("docker compose", str(caught.exception))
        self.assertFalse(client.available())
        self.assertTrue(self.client.available())

    def test_json_disabled_is_explained(self) -> None:
        client = SearchClient(transport=httpx.MockTransport(lambda request: httpx.Response(403, text="no")))
        with self.assertRaises(SearchUnavailable) as caught:
            client.search("anything")
        self.assertIn("search.formats", str(caught.exception))


class ActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.action = WebSearchAction(SearchClient(transport=RecordingTransport()))

    def test_general_results_are_links_with_snippets_and_a_cited_message(self) -> None:
        validation = self.action.validate(ActionRequest(action="web_search", arguments={"query": " platypus venom ", "max_results": 5}), object())
        self.assertTrue(validation.ok)
        self.assertEqual(validation.resolved_arguments["query"], "platypus venom")
        result = self.action.execute(ActionRequest(action="web_search", arguments=validation.resolved_arguments), object())
        self.assertEqual(result.status, "success")
        self.assertIn("1. Platypus venom — https://example.org/platypus (2026-01-02)", result.message)
        self.assertIn("Males have a spur.", result.message)
        self.assertIn("Engines that did not answer: startpage (timeout)", result.message)
        kinds = [item.kind for item in result.results]
        self.assertEqual(kinds, [ResultKind.LINK, ResultKind.LINK, ResultKind.LINK, ResultKind.STATUS])
        first = result.results[0]
        self.assertEqual(first.source.kind, "web")
        self.assertEqual(first.source.ref, "https://example.org/platypus")
        self.assertEqual(first.data["description"], "Males have a spur.")
        self.assertEqual(first.data["date"], "2026-01-02")

    def test_image_results_are_images_that_open_the_page(self) -> None:
        result = self.action.execute(ActionRequest(action="web_search", arguments={"query": "platypus", "category": "images", "max_results": 5}), object())
        self.assertEqual(result.status, "success")
        self.assertEqual(result.results[0].kind, ResultKind.IMAGE)
        self.assertEqual(result.results[0].data["uri"], "https://cdn.example.org/t.png")
        self.assertEqual(result.results[0].data["mime_type"], "image/png")
        self.assertEqual(result.results[0].source.ref, "https://example.org/photo")

    def test_the_action_is_read_only_and_outbound(self) -> None:
        self.assertEqual(self.action.definition.permission.value, "read")
        self.assertTrue(self.action.definition.outbound)
        self.assertFalse(self.action.definition.requires_confirmation)

    def test_a_stopped_search_is_a_failed_result_with_the_start_hint(self) -> None:
        action = WebSearchAction(SearchClient(transport=httpx.MockTransport(_down)))
        result = action.execute(ActionRequest(action="web_search", arguments={"query": "anything"}), object())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "search_unavailable")
        self.assertIn("docker compose", result.message)

    def test_arguments_are_validated(self) -> None:
        validation = self.action.validate(ActionRequest(action="web_search", arguments={"query": "x", "category": "maps"}), object())
        self.assertFalse(validation.ok)
        self.assertIn("Invalid arguments", validation.error or "")
        empty = self.action.validate(ActionRequest(action="web_search", arguments={"query": "   "}), object())
        self.assertFalse(empty.ok)


if __name__ == "__main__":
    unittest.main()
