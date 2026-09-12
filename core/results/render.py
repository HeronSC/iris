# File: core/results/render.py

from __future__ import annotations

from typing import Any, Iterable

from core.application.contracts import DetailContent
from core.knowledge.models import MemoryKind, MemoryRecord
from core.results.models import Result, ResultKind, to_json_list

MAX_TABLE_ROWS = 50


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if abs(value) < 1e15 else str(value)
    return str(value).replace("|", "\\|").replace("\n", " ")


def _heading(result: Result) -> str:
    return f"**{result.title}**\n\n" if result.title else ""


def render_markdown(result: Result) -> str:
    data = result.data
    if result.kind is ResultKind.TEXT:
        return _heading(result) + str(data["text"])
    if result.kind is ResultKind.TABLE:
        columns = data["columns"]
        rows = data["rows"]
        lines = ["| " + " | ".join(_cell(item) for item in columns) + " |", "|" + "|".join(" --- " for _ in columns) + "|"]
        for row in rows[:MAX_TABLE_ROWS]:
            lines.append("| " + " | ".join(_cell(item) for item in row) + " |")
        if len(rows) > MAX_TABLE_ROWS:
            lines.append(f"\n{len(rows) - MAX_TABLE_ROWS} more row(s) not shown")
        return _heading(result) + "\n".join(lines)
    if result.kind is ResultKind.IMAGE:
        alt = data.get("alt") or result.title or "image"
        uri = data.get("uri") or f"data:{data['mime_type']};base64,{data.get('base64', '')}"
        return _heading(result) + f"![{alt}]({uri})"
    if result.kind is ResultKind.VIDEO:
        return _heading(result) + f"Video: {data['uri']}"
    if result.kind is ResultKind.FILE:
        size = data.get("size_bytes")
        detail = f" ({size / 1024:.1f} KB)" if isinstance(size, int) else ""
        return _heading(result) + f"File: `{data['path']}`{detail}"
    if result.kind is ResultKind.CODE:
        path = f"`{data['path']}`\n\n" if data.get("path") else ""
        return _heading(result) + path + f"```{data.get('language', '')}\n{data['code']}\n```"
    if result.kind is ResultKind.DIFF:
        path = f"`{data['path']}`\n\n" if data.get("path") else ""
        return _heading(result) + path + f"```diff\n{data['unified']}\n```"
    if result.kind is ResultKind.LINK:
        label = result.title or data["url"]
        line = f"[{label}]({data['url']})"
        byline = ", ".join(part for part in (data.get("author") and f"by {data['author']}", data.get("date") and f"dated {data['date']}") if part)
        if byline:
            line += f" — {byline}"
        if data.get("description"):
            line += f"\n\n{data['description']}"
        return line
    if result.kind is ResultKind.CHART:
        labels = data.get("labels")
        lines = [f"{data['chart_type'].title()} chart" + (f" ({data['units']})" if data.get("units") else "")]
        for series in data["series"]:
            values = series.get("values", [])
            name = series.get("name", "series")
            if labels and len(labels) == len(values):
                pairs = ", ".join(f"{_cell(label)}: {_cell(value)}" for label, value in zip(labels, values))
            else:
                pairs = ", ".join(_cell(value) for value in values)
            lines.append(f"- {name}: {pairs}")
        return _heading(result) + "\n".join(lines)
    if result.kind is ResultKind.STATUS:
        marker = {"ok": "OK", "warning": "Warning", "error": "Error", "pending": "Pending"}[data["state"]]
        details = data.get("details") or {}
        extra = "".join(f"\n- {key}: {_cell(value)}" for key, value in details.items())
        return _heading(result) + f"{marker}: {data['message']}{extra}"
    return _heading(result) + str(data)


def render_all(results: Iterable[Result], *, separator: str = "\n\n") -> str:
    return separator.join(render_markdown(item) for item in results)


def render_plain(result: Result) -> str:
    if result.kind is ResultKind.TEXT:
        return str(result.data["text"])
    if result.kind is ResultKind.LINK:
        title = result.title or result.data["url"]
        return f"{title} — {result.data['url']}"
    if result.kind is ResultKind.STATUS:
        return f"{result.data['state']}: {result.data['message']}"
    return render_markdown(result)


def to_detail(results: Iterable[Result], *, title: str | None = None, section_id: str | None = None) -> DetailContent:
    items = list(results)
    first = items[0] if items else None
    return DetailContent(
        type="markdown",
        section_id=section_id,
        title=title or (first.title if first is not None else None),
        content=render_all(items),
        metadata={"results": to_json_list(items), "kinds": sorted({item.kind.value for item in items})},
    )


def to_memory_record(result: Result, *, topic: str, kind: MemoryKind = MemoryKind.OBSERVATION) -> MemoryRecord:
    return MemoryRecord(
        kind=kind,
        topic=topic,
        content=render_plain(result)[:2000],
        source=f"{result.source.kind}:{result.source.name}",
        source_ref=result.source.ref,
        data={"result": result.to_json()},
        occurred_at=result.created_at,
    )


__all__ = ["MAX_TABLE_ROWS", "render_all", "render_markdown", "render_plain", "to_detail", "to_memory_record"]
