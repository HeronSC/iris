# File: core/actions/implementations/web_search.py

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.results.models import Result, Source, image, link, status
from core.tools.models import PermissionLevel, ToolDefinition
from core.web.search import SearchClient, SearchHit, SearchResponse, SearchUnavailable

IMAGE_TYPES = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


class WebSearchArguments(BaseModel):
    query: str = Field(description="What to search the web for, as a short search phrase")
    category: Literal["general", "images", "videos", "news"] = Field(default="general", description="general for web pages and documentation, images, videos, or news for current events")
    max_results: int = Field(default=8, ge=1, le=20, description="How many results to return")
    time_range: Literal["day", "week", "month", "year"] | None = Field(default=None, description="Only results from this recent period, when recency matters")


def _mime_for(url: str) -> str:
    lowered = url.lower().split("?", 1)[0]
    for suffix, mime in IMAGE_TYPES.items():
        if lowered.endswith(suffix):
            return mime
    return "image/jpeg"


def _hit_result(hit: SearchHit, category: str) -> Result:
    source = Source("web_search", "web", hit.url)
    if category == "images" and hit.image_url:
        return image(mime_type=_mime_for(hit.image_url), uri=hit.thumbnail or hit.image_url, source=source, alt=hit.title, title=hit.title)
    return link(hit.url, source=source, title=hit.title, description=hit.snippet or None, date=hit.published)


def render_search(response: SearchResponse) -> str:
    lines = [response.source_line]
    if not response.hits:
        lines.append("No results.")
    for index, hit in enumerate(response.hits, start=1):
        head = f"{index}. {hit.title} — {hit.url}"
        if hit.published:
            head += f" ({hit.published})"
        lines.append(head)
        if hit.snippet:
            lines.append(f"   {hit.snippet[:300]}")
    if response.suggestions:
        lines.append("Related: " + ", ".join(response.suggestions))
    if response.unresponsive:
        lines.append("Engines that did not answer: " + ", ".join(response.unresponsive))
    return "\n".join(lines)


class WebSearchAction:

    name = "web_search"
    definition = ToolDefinition(
        name="web_search",
        description=(
            "Search the web through the local SearXNG instance for pages, documentation, images, videos, or news. "
            "Returns titles, links, and snippets with the source cited; follow with fetch_web_page to read a result."
        ),
        arguments=WebSearchArguments,
        permission=PermissionLevel.READ,
        outbound=True,
        timeout_seconds=30.0,
        cost="seconds; the query leaves this machine through SearXNG",
        keywords=("search", "google", "look up", "look online", "find online", "web search", "on the web", "images of", "pictures of", "videos of", "news about", "latest news", "documentation for"),
    )

    def __init__(self, client: SearchClient | None = None) -> None:
        self.client = client or SearchClient()

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = WebSearchArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        query = " ".join(arguments.query.split())
        if not query:
            return ValidationResult(ok=False, error="The search query is empty")
        return ValidationResult(
            ok=True,
            resolved_target=f"{arguments.category}: {query}",
            resolved_arguments={"query": query, "category": arguments.category, "max_results": arguments.max_results, "time_range": arguments.time_range},
        )

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        arguments = request.arguments
        query = str(arguments.get("query", ""))
        category = str(arguments.get("category") or "general")
        try:
            response = self.client.search(
                query,
                category=category,
                time_range=arguments.get("time_range") or None,
                max_results=int(arguments.get("max_results", 8)),
            )
        except SearchUnavailable as error:
            return ActionResult(status="failed", message=str(error), action=self.name, resolved_target=query, error="search_unavailable")
        except ValueError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, resolved_target=query, error="invalid_arguments")
        results: list[Result] = [_hit_result(hit, category) for hit in response.hits]
        if response.unresponsive:
            results.append(
                status(
                    "warning",
                    "Some search engines did not answer",
                    source=Source("web_search", "web", f"{self.client.base_url}/search"),
                    details={"engines": ", ".join(response.unresponsive)},
                )
            )
        if not response.hits:
            results.append(status("warning", f"No {category} results for \"{query}\"", source=Source("web_search", "web", f"{self.client.base_url}/search")))
        return ActionResult(
            status="success",
            message=render_search(response),
            action=self.name,
            resolved_target=f"{category}: {query}",
            results=tuple(results),
        )


__all__ = ["WebSearchAction", "WebSearchArguments", "render_search"]
