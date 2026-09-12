# File: core/results/mcp.py

from __future__ import annotations

import json
from typing import Any, Iterable

from core.results.models import Result, ResultKind, Source, code, file, image, link, text
from core.results.render import render_markdown

FENCE = "```"


def _attr(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        value = getattr(item, name, None)
        if value is not None:
            return value
    return default


def _text_result(body: str, source: Source) -> Result:
    stripped = body.strip()
    if stripped.startswith(FENCE) and stripped.endswith(FENCE) and stripped.count(FENCE) == 2:
        first_line, _, rest = stripped[len(FENCE):-len(FENCE)].partition("\n")
        return code(rest.rstrip("\n"), source=source, language=first_line.strip() or None)
    return text(body, source=source)


def _resource_result(resource: Any, source: Source) -> Result | None:
    uri = str(_attr(resource, "uri", default="") or "")
    mime = str(_attr(resource, "mime_type", "mimeType", default="") or "")
    body = _attr(resource, "text")
    blob = _attr(resource, "blob")
    if isinstance(body, str):
        if mime.startswith("text/") or mime in {"application/json", ""}:
            return text(body, source=Source(source.name, source.kind, uri or None), title=uri or None)
        return code(body, source=Source(source.name, source.kind, uri or None), language=mime.rsplit("/", 1)[-1], path=uri or None)
    if isinstance(blob, str) and mime.startswith("image/"):
        return image(mime_type=mime, base64=blob, source=Source(source.name, source.kind, uri or None), title=uri or None)
    if uri.startswith(("http://", "https://")):
        return link(uri, source=source, title=_attr(resource, "name", "title"))
    if uri:
        return file(uri.removeprefix("file://"), source=Source(source.name, source.kind, uri), mime_type=mime or None)
    return None


def from_mcp(content: Iterable[Any] | None, *, source: Source, structured: Any = None) -> tuple[Result, ...]:
    found: list[Result] = []
    for item in content or []:
        block_type = str(_attr(item, "type", default="") or "")
        if block_type == "text":
            body = _attr(item, "text", default="")
            if isinstance(body, str) and body.strip():
                found.append(_text_result(body, source))
        elif block_type == "image":
            data = _attr(item, "data")
            mime = str(_attr(item, "mime_type", "mimeType", default="image/png") or "image/png")
            if isinstance(data, str) and data:
                found.append(image(mime_type=mime, base64=data, source=source))
        elif block_type == "resource":
            result = _resource_result(_attr(item, "resource", default={}), source)
            if result is not None:
                found.append(result)
        elif block_type == "resource_link":
            uri = str(_attr(item, "uri", default="") or "")
            if uri:
                found.append(link(uri, source=source, title=_attr(item, "name", "title"), description=_attr(item, "description")))
    if not found and structured is not None:
        found.append(code(json.dumps(structured, ensure_ascii=False, indent=2), source=source, language="json"))
    return tuple(found)


def to_mcp(results: Iterable[Result]) -> list[Any]:
    #! @allow-local-import
    from mcp.types import ImageContent, TextContent

    blocks: list[Any] = []
    for item in results:
        if item.kind is ResultKind.IMAGE and item.data.get("base64"):
            blocks.append(ImageContent(type="image", data=item.data["base64"], mime_type=item.data["mime_type"]))
            continue
        blocks.append(TextContent(type="text", text=render_markdown(item)))
    return blocks


__all__ = ["from_mcp", "to_mcp"]
