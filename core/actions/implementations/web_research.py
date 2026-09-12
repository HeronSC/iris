# File: core/actions/implementations/web_research.py

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.results.models import Result, Source, link, status
from core.results.models import text as text_result
from core.tools.models import PermissionLevel, ToolDefinition
from core.web.fetch import PageFetcher, UrlRejected
from core.web.search import SearchClient, SearchUnavailable

MAX_CHARS_PER_PAGE = 2500


class ResearchArguments(BaseModel):
    query: str = Field(description="The question or topic to research on the web")
    pages: int = Field(default=3, ge=1, le=6, description="How many of the top results to read in full")
    time_range: str | None = Field(default=None, description="day, week, month or year when recency matters")


class WebResearchAction:

    name = "web_research"
    definition = ToolDefinition(
        name="web_research",
        description=(
            "Research a question on the web: search, read the top pages, and return their text with numbered sources. "
            "Answer from these pages only, cite each claim as [n], and say where the sources disagree or where none of them answers."
        ),
        arguments=ResearchArguments,
        permission=PermissionLevel.READ,
        outbound=True,
        timeout_seconds=90.0,
        cost="half a minute; the query and page fetches leave this machine",
        keywords=("research", "find out about", "what do sources say", "look into", "compare sources", "dig into", "read up on"),
    )

    def __init__(self, client: SearchClient | None = None, fetcher: PageFetcher | None = None) -> None:
        self.client = client or SearchClient()
        self.fetcher = fetcher or PageFetcher()

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = ResearchArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        query = " ".join(arguments.query.split())
        if not query:
            return ValidationResult(ok=False, error="The research question is empty")
        if arguments.time_range and arguments.time_range not in {"day", "week", "month", "year"}:
            return ValidationResult(ok=False, error="time_range is day, week, month or year")
        return ValidationResult(ok=True, resolved_target=query, resolved_arguments={"query": query, "pages": arguments.pages, "time_range": arguments.time_range})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        query = str(request.arguments.get("query") or "")
        wanted = int(request.arguments.get("pages") or 3)
        try:
            response = self.client.search(query, category="general", time_range=request.arguments.get("time_range") or None, max_results=wanted * 2)
        except SearchUnavailable as error:
            return ActionResult(status="failed", message=str(error), action=self.name, resolved_target=query, error="search_unavailable")
        source = Source("web_research", "web", query)
        if not response.hits:
            message = f"No web results for '{query}'."
            return ActionResult(status="success", message=message, action=self.name, results=(status("warning", message, source=source),))
        results: list[Result] = []
        lines = [f"Sources for '{query}' (answer from these, cite as [n], say where they disagree):"]
        read = 0
        skipped: list[str] = []
        for hit in response.hits:
            if read >= wanted:
                break
            try:
                page = self.fetcher.fetch(hit.url)
            except (UrlRejected, Exception) as error:
                skipped.append(f"{hit.url} ({str(error)[:60]})")
                continue
            if not page.text.strip():
                skipped.append(f"{hit.url} (no readable text)")
                continue
            read += 1
            body = page.text.strip()
            truncated = len(body) > MAX_CHARS_PER_PAGE
            excerpt = body[:MAX_CHARS_PER_PAGE].rstrip() + (" …" if truncated else "")
            title = page.title or hit.title or page.final_url
            byline = ", ".join(part for part in (page.author and f"by {page.author}", page.date and f"dated {page.date}") if part)
            lines.append(f"[{read}] {title} — {page.final_url}" + (f" ({byline})" if byline else ""))
            lines.append(excerpt)
            lines.append("")
            page_source = Source("web_research", "web", page.final_url)
            results.append(link(page.final_url, source=page_source, title=f"[{read}] {title}", description=hit.snippet or None, author=page.author or None, date=page.date or None))
            results.append(text_result(excerpt, source=page_source, title=f"[{read}] {title}", format="text"))
        if read == 0:
            message = f"Found results for '{query}' but none could be read: " + "; ".join(skipped[:4])
            return ActionResult(status="success", message=message, action=self.name, results=(status("warning", message, source=source),))
        if skipped:
            lines.append("Not read: " + "; ".join(skipped[:4]))
            results.append(status("warning", f"{len(skipped)} result{'s' if len(skipped) != 1 else ''} could not be read", source=source, details={"skipped": "; ".join(skipped[:4])}))
        return ActionResult(status="success", message="\n".join(lines).rstrip(), action=self.name, resolved_target=query, results=tuple(results))


__all__ = ["MAX_CHARS_PER_PAGE", "WebResearchAction"]
