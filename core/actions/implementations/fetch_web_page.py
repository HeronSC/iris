# File: core/actions/implementations/fetch_web_page.py

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.results.models import Source, link
from core.results.models import text as text_result
from core.tools.models import PermissionLevel, ToolDefinition
from core.web.fetch import PageFetcher, UrlRejected


class FetchWebPageArguments(BaseModel):
    url: str = Field(description="The web address to read, exactly as the user gave it")
    max_chars: int = Field(default=6000, ge=200, le=40000, description="How much of the page text to return")


class FetchWebPageAction:

    name = "fetch_web_page"
    definition = ToolDefinition(
        name="fetch_web_page",
        description="Fetch a web page by URL and return its readable text, title, author, and date, with the source cited. Use only for a URL the user gave.",
        arguments=FetchWebPageArguments,
        permission=PermissionLevel.READ,
        outbound=True,
        keywords=("fetch", "http", "https", "www", "url", "link", "website", "web page", "webpage", "article", "read this"),
    )

    def __init__(self, fetcher: PageFetcher | None = None) -> None:
        self.fetcher = fetcher or PageFetcher()

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = FetchWebPageArguments.model_validate(request.arguments)
            url = self.fetcher.check_url(arguments.url)
        except UrlRejected as error:
            return ValidationResult(ok=False, error=str(error))
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        return ValidationResult(ok=True, resolved_target=url, resolved_arguments={"url": url, "max_chars": arguments.max_chars})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        url = str(request.arguments.get("url", ""))
        max_chars = int(request.arguments.get("max_chars", 6000))
        try:
            page = self.fetcher.fetch(url)
        except UrlRejected as error:
            return ActionResult(status="failed", message=str(error), action=self.name, resolved_target=url, error="url_rejected")
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ActionResult(status="failed", message=f"Could not fetch {url}: {error}", action=self.name, resolved_target=url, error="fetch_failed")
        if not page.text.strip():
            return ActionResult(
                status="failed",
                message=f"{page.source_line}\nThe page returned no readable text (content type {page.content_type or 'unknown'}).",
                action=self.name,
                resolved_target=page.final_url,
                error="no_text",
            )
        lines: list[str] = []
        if page.title:
            lines.append(page.title)
        lines.append(page.source_line)
        byline = ", ".join(part for part in (page.author and f"by {page.author}", page.date and f"dated {page.date}") if part)
        if byline:
            lines.append(byline[0].upper() + byline[1:])
        text = page.text
        truncated = len(text) > max_chars
        lines.append("")
        lines.append(text[:max_chars].rstrip() + (" …" if truncated else ""))
        if truncated:
            lines.append(f"\n[{len(text) - max_chars} more characters not shown]")
        results = (
            link(
                page.final_url,
                source=Source("fetch_web_page", "web", page.final_url),
                title=page.title or page.final_url,
                author=page.author or None,
                date=page.date or None,
            ),
            text_result(
                text[:max_chars].rstrip() + (" …" if truncated else ""),
                source=Source("fetch_web_page", "web", page.final_url),
                title=page.title or None,
                format="text",
            ),
        )
        return ActionResult(status="success", message="\n".join(lines), action=self.name, resolved_target=page.final_url, results=results)
