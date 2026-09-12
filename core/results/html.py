# File: core/results/html.py

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import PurePath
from typing import Any
from urllib.parse import quote

from core.results.models import Result, ResultKind

MAX_TABLE_ROWS = 200

OPEN_SCHEME = "iris://open"
CONFIRM_URL = "iris://confirm"
CANCEL_URL = "iris://cancel"

MarkdownConverter = Callable[[str], str]

CHART_COLORS = ("#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#14b8a6")

STYLE = """
:root {
  --bg: #fafaf9; --fg: #1c1917; --muted: #57534e; --border: #d6d3d1; --card: #ffffff;
  --accent: #2563eb; --ok: #15803d; --warning: #b45309; --error: #b91c1c; --pending: #6d28d9;
  --add-bg: #dcfce7; --add-fg: #14532d; --del-bg: #fee2e2; --del-fg: #7f1d1d; --hunk-bg: #e0e7ff; --code-bg: #f5f5f4;
}
html[data-theme="dark"] {
  --bg: #1c1917; --fg: #e7e5e4; --muted: #a8a29e; --border: #44403c; --card: #292524;
  --accent: #60a5fa; --ok: #4ade80; --warning: #fbbf24; --error: #f87171; --pending: #c4b5fd;
  --add-bg: #14532d; --add-fg: #dcfce7; --del-bg: #7f1d1d; --del-fg: #fee2e2; --hunk-bg: #312e81; --code-bg: #1f1d1b;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 12px 16px; background: var(--bg); color: var(--fg); font: 14px/1.5 "Segoe UI", system-ui, sans-serif; }
a { color: var(--accent); }
h1, h2, h3, h4 { margin: 0.4em 0 0.3em; line-height: 1.25; }
h2.panel-title { font-size: 1.15em; color: var(--muted); font-weight: 600; margin-bottom: 0.8em; }
.result { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin: 0 0 12px; }
.result-header { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; margin-bottom: 6px; }
.result-title { font-weight: 600; font-size: 1.05em; }
.result-source { color: var(--muted); font-size: 0.85em; margin-left: auto; white-space: nowrap; }
.result-source a { color: var(--muted); }
.result-body p:first-child { margin-top: 0; }
.result-body p:last-child { margin-bottom: 0; }
table.result-table { border-collapse: collapse; width: 100%; font-size: 0.95em; }
table.result-table th, table.result-table td { border: 1px solid var(--border); padding: 4px 8px; text-align: left; vertical-align: top; }
table.result-table th { background: var(--code-bg); }
table.result-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.table-note { color: var(--muted); font-size: 0.85em; margin-top: 4px; }
pre { background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; overflow-x: auto; font: 12.5px/1.45 Consolas, "Cascadia Mono", monospace; margin: 0; }
pre.diff { padding: 0; }
pre.diff span { display: block; padding: 0 10px; }
pre.diff .add { background: var(--add-bg); color: var(--add-fg); }
pre.diff .del { background: var(--del-bg); color: var(--del-fg); }
pre.diff .hunk { background: var(--hunk-bg); }
pre.diff .meta { color: var(--muted); font-weight: 600; }
.path { font-family: Consolas, "Cascadia Mono", monospace; font-size: 0.9em; color: var(--muted); margin-bottom: 4px; }
img.result-image { max-width: 100%; height: auto; border-radius: 6px; border: 1px solid var(--border); }
video.result-video { max-width: 100%; border-radius: 6px; }
.file-card { display: flex; gap: 12px; align-items: center; }
.file-name { font-weight: 600; }
.file-meta { color: var(--muted); font-size: 0.85em; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.8em; font-weight: 600; border: 1px solid currentColor; }
.badge-ok { color: var(--ok); } .badge-warning { color: var(--warning); } .badge-error { color: var(--error); } .badge-pending { color: var(--pending); }
.status-details { margin: 6px 0 0; padding-left: 1.2em; color: var(--muted); }
.link-byline, .link-description { color: var(--muted); font-size: 0.9em; }
.chart svg { max-width: 100%; height: auto; display: block; }
.chart-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.85em; color: var(--muted); margin-top: 4px; }
.chart-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
.confirmation { border: 2px solid var(--warning); border-radius: 8px; padding: 12px 14px; margin: 0 0 14px; background: var(--card); }
.confirmation.irreversible { border-color: var(--error); }
.confirmation-title { font-weight: 700; font-size: 1.1em; margin-bottom: 4px; }
.confirmation-summary { margin-bottom: 8px; }
.confirmation-row { color: var(--muted); font-size: 0.9em; margin: 2px 0; }
.confirmation-actions { display: flex; gap: 10px; margin-top: 10px; }
.confirmation-actions a { text-decoration: none; padding: 6px 16px; border-radius: 6px; font-weight: 600; border: 1px solid var(--border); color: var(--fg); background: var(--code-bg); }
.confirmation-actions a.approve { background: var(--accent); color: #fff; border-color: var(--accent); }
.section { margin-bottom: 14px; }
hr.section-break { border: 0; border-top: 1px solid var(--border); margin: 12px 0; }
.empty { color: var(--muted); font-style: italic; }
"""


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def open_url(path: str) -> str:
    return f"{OPEN_SCHEME}?path={quote(str(path), safe='')}"


def _is_web_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value, flags=re.IGNORECASE))


def _looks_like_path(value: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/)", value))


def _ref_link(ref: str, label: str) -> str:
    if _is_web_url(ref):
        return f'<a href="{escape(ref)}" title="{escape(ref)}">{escape(label)}</a>'
    if _looks_like_path(ref):
        return f'<a href="{escape(open_url(ref))}" title="{escape(ref)}">{escape(label)}</a>'
    return f'<span title="{escape(ref)}">{escape(label)}</span>'


def _source_html(result: Result) -> str:
    source = result.source
    label = f"{source.kind}: {source.name}" if source.kind and source.kind != "tool" else source.name
    body = _ref_link(source.ref, label) if source.ref else escape(label)
    stamp = escape(result.created_at.replace("T", " ").replace("+00:00", " UTC"))
    return f'<span class="result-source">{body} · <time datetime="{escape(result.created_at)}">{stamp}</time></span>'


def _paragraphs(content: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", content.strip()) if block.strip()]
    return "".join(f"<p>{escape(block).replace(chr(10), '<br>')}</p>" for block in blocks) or '<p class="empty">Nothing to show.</p>'


def _file_url(value: str) -> str:
    if _looks_like_path(value):
        return "file:///" + quote(str(PurePath(value).as_posix()).lstrip("/"), safe="/:")
    return value


def _size(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    size = float(value)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if abs(value) < 1e15 else str(value)
    return str(value)


def _render_text(result: Result, markdown: MarkdownConverter | None) -> str:
    content = str(result.data["text"])
    if result.data.get("format", "markdown") == "markdown" and markdown is not None:
        return markdown(content)
    return _paragraphs(content)


def _render_table(result: Result) -> str:
    columns: Sequence[Any] = result.data["columns"]
    rows: Sequence[Sequence[Any]] = result.data["rows"]
    head = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body: list[str] = []
    for row in rows[:MAX_TABLE_ROWS]:
        cells = "".join(f'<td class="num">{escape(_cell(value))}</td>' if _is_number(value) else f"<td>{escape(_cell(value))}</td>" for value in row)
        body.append(f"<tr>{cells}</tr>")
    note = f'<div class="table-note">{len(rows) - MAX_TABLE_ROWS} more row(s) not shown</div>' if len(rows) > MAX_TABLE_ROWS else ""
    return f'<table class="result-table"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>{note}'


def _render_image(result: Result) -> str:
    data = result.data
    uri = data.get("uri") or f"data:{data['mime_type']};base64,{data.get('base64', '')}"
    alt = data.get("alt") or result.title or "image"
    src = _file_url(str(uri))
    image = f'<img class="result-image" src="{escape(src)}" alt="{escape(alt)}">'
    if _looks_like_path(str(uri)):
        return f'<a href="{escape(open_url(str(uri)))}" title="Open {escape(uri)}">{image}</a>'
    return image


def _render_video(result: Result) -> str:
    uri = str(result.data["uri"])
    mime = result.data.get("mime_type")
    source = f'<source src="{escape(_file_url(uri))}"' + (f' type="{escape(mime)}"' if mime else "") + ">"
    fallback = _ref_link(uri, uri) if _is_web_url(uri) or _looks_like_path(uri) else escape(uri)
    return f'<video class="result-video" controls preload="metadata">{source}</video><div class="path">{fallback}</div>'


def _render_file(result: Result) -> str:
    data = result.data
    path = str(data["path"])
    name = str(data.get("name") or PurePath(path).name or path)
    folder = str(PurePath(path).parent)
    meta = " · ".join(part for part in (_size(data.get("size_bytes")), str(data.get("mime_type") or "")) if part)
    links = f'<a href="{escape(open_url(path))}">Open</a> · <a href="{escape(open_url(folder))}">Folder</a>'
    return (
        '<div class="file-card"><div>'
        f'<div class="file-name">{escape(name)}</div>'
        f'<div class="path">{escape(path)}</div>'
        + (f'<div class="file-meta">{escape(meta)}</div>' if meta else "")
        + f'</div><div class="file-meta">{links}</div></div>'
    )


def _render_code(result: Result) -> str:
    data = result.data
    language = str(data.get("language") or "")
    path = f'<div class="path">{_ref_link(str(data["path"]), str(data["path"]))}</div>' if data.get("path") else ""
    css = f' class="language-{escape(language)}"' if language else ""
    return f"{path}<pre><code{css}>{escape(data['code'])}</code></pre>"


def diff_lines_html(unified: str) -> str:
    rendered: list[str] = []
    for line in unified.splitlines():
        if line.startswith(("+++", "---")):
            css = "meta"
        elif line.startswith("@@"):
            css = "hunk"
        elif line.startswith("+"):
            css = "add"
        elif line.startswith("-"):
            css = "del"
        else:
            css = "ctx"
        rendered.append(f'<span class="{css}">{escape(line) or "&nbsp;"}</span>')
    return f'<pre class="diff">{"".join(rendered)}</pre>'


def _render_diff(result: Result) -> str:
    data = result.data
    path = f'<div class="path">{_ref_link(str(data["path"]), str(data["path"]))}</div>' if data.get("path") else ""
    return path + diff_lines_html(str(data["unified"]))


def _render_link(result: Result) -> str:
    data = result.data
    url = str(data["url"])
    label = result.title or url
    byline = ", ".join(part for part in (data.get("author") and f"by {data['author']}", data.get("date") and f"dated {data['date']}") if part)
    parts = [f'<div><a href="{escape(url)}">{escape(label)}</a></div>', f'<div class="path">{escape(url)}</div>']
    if byline:
        parts.append(f'<div class="link-byline">{escape(byline)}</div>')
    if data.get("description"):
        parts.append(f'<div class="link-description">{escape(data["description"])}</div>')
    return "".join(parts)


def _chart_svg(chart_type: str, series: Sequence[dict[str, Any]], labels: Sequence[Any] | None, units: str | None) -> str:
    width, height, left, bottom, top = 640, 260, 48, 36, 12
    values = [float(value) for item in series for value in item.get("values", []) if _is_number(value)]
    if not values:
        return '<p class="empty">No data points.</p>'
    high = max(max(values), 0.0)
    low = min(min(values), 0.0)
    span = (high - low) or 1.0
    plot_w, plot_h = width - left - 12, height - top - bottom
    y_of = lambda value: top + (high - value) / span * plot_h
    points = max(len(item.get("values", [])) for item in series)
    axis_labels = list(labels) if labels and len(labels) == points else [str(index + 1) for index in range(points)]
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img">']
    for step in range(5):
        value = low + span * step / 4
        y = y_of(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - 12}" y2="{y:.1f}" stroke="currentColor" stroke-opacity="0.15"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="currentColor">{escape(_cell(round(value, 2)))}</text>')
    if chart_type in {"line", "area"}:
        step_x = plot_w / max(points - 1, 1)
        for index, item in enumerate(series):
            color = CHART_COLORS[index % len(CHART_COLORS)]
            coords = [f"{left + i * step_x:.1f},{y_of(float(v)):.1f}" for i, v in enumerate(item.get("values", [])) if _is_number(v)]
            parts.append(f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" stroke-width="2"/>')
            parts.extend(f'<circle cx="{c.split(",")[0]}" cy="{c.split(",")[1]}" r="3" fill="{color}"/>' for c in coords)
        for i, label in enumerate(axis_labels):
            parts.append(f'<text x="{left + i * step_x:.1f}" y="{height - 14}" text-anchor="middle" font-size="11" fill="currentColor">{escape(label)}</text>')
    else:
        group_w = plot_w / max(points, 1)
        bar_w = max(group_w * 0.8 / max(len(series), 1), 2)
        zero = y_of(0.0)
        for index, item in enumerate(series):
            color = CHART_COLORS[index % len(CHART_COLORS)]
            for i, value in enumerate(item.get("values", [])):
                if not _is_number(value):
                    continue
                x = left + i * group_w + group_w * 0.1 + index * bar_w
                y = y_of(float(value))
                parts.append(f'<rect x="{x:.1f}" y="{min(y, zero):.1f}" width="{bar_w:.1f}" height="{abs(zero - y):.1f}" fill="{color}"><title>{escape(item.get("name", "series"))}: {escape(_cell(value))}</title></rect>')
        for i, label in enumerate(axis_labels):
            parts.append(f'<text x="{left + i * group_w + group_w / 2:.1f}" y="{height - 14}" text-anchor="middle" font-size="11" fill="currentColor">{escape(label)}</text>')
    if units:
        parts.append(f'<text x="{width - 12}" y="{height - 2}" text-anchor="end" font-size="11" fill="currentColor">{escape(units)}</text>')
    parts.append("</svg>")
    legend = "".join(
        f'<span><i style="background:{CHART_COLORS[index % len(CHART_COLORS)]}"></i>{escape(item.get("name", "series"))}</span>' for index, item in enumerate(series)
    )
    return f'<div class="chart">{"".join(parts)}<div class="chart-legend">{legend}</div></div>'


def _render_chart(result: Result) -> str:
    data = result.data
    return _chart_svg(str(data["chart_type"]), data["series"], data.get("labels"), data.get("units"))


def _render_status(result: Result) -> str:
    data = result.data
    state = str(data["state"])
    details = data.get("details") or {}
    extra = "".join(f"<li>{escape(key)}: {escape(_cell(value))}</li>" for key, value in details.items())
    return (
        f'<span class="badge badge-{escape(state)}">{escape(state.title())}</span> {escape(data["message"])}'
        + (f'<ul class="status-details">{extra}</ul>' if extra else "")
    )


def render_body(result: Result, *, markdown: MarkdownConverter | None = None) -> str:
    if result.kind is ResultKind.TEXT:
        return _render_text(result, markdown)
    if result.kind is ResultKind.TABLE:
        return _render_table(result)
    if result.kind is ResultKind.IMAGE:
        return _render_image(result)
    if result.kind is ResultKind.VIDEO:
        return _render_video(result)
    if result.kind is ResultKind.FILE:
        return _render_file(result)
    if result.kind is ResultKind.CODE:
        return _render_code(result)
    if result.kind is ResultKind.DIFF:
        return _render_diff(result)
    if result.kind is ResultKind.LINK:
        return _render_link(result)
    if result.kind is ResultKind.CHART:
        return _render_chart(result)
    if result.kind is ResultKind.STATUS:
        return _render_status(result)
    return f"<pre>{escape(result.data)}</pre>"


def render_result(result: Result, *, markdown: MarkdownConverter | None = None) -> str:
    title = f'<span class="result-title">{escape(result.title)}</span>' if result.title else ""
    return (
        f'<article class="result result-{result.kind.value}" id="result-{escape(result.id)}">'
        f'<div class="result-header">{title}{_source_html(result)}</div>'
        f'<div class="result-body">{render_body(result, markdown=markdown)}</div>'
        "</article>"
    )


def render_results(results: Iterable[Result], *, markdown: MarkdownConverter | None = None) -> str:
    rendered = [render_result(item, markdown=markdown) for item in results]
    return "".join(rendered) if rendered else '<p class="empty">No results.</p>'


def render_approval_bar() -> str:
    return (
        '<section class="confirmation approval-bar"><div class="confirmation-summary">An action is waiting for your approval. Read it below, then choose.</div>'
        f'<div class="confirmation-actions"><a class="approve" href="{CONFIRM_URL}">Approve</a><a class="cancel" href="{CANCEL_URL}">Cancel</a></div></section>'
    )


def render_confirmation(preview: dict[str, Any], *, actions: bool = True) -> str:
    summary = str(preview.get("summary") or "An action is awaiting approval.")
    title = str(preview.get("title") or "Approval needed")
    irreversible = bool(preview.get("irreversible"))
    rows: list[str] = []
    if preview.get("target"):
        rows.append(f'<div class="confirmation-row">Target: {escape(preview["target"])}</div>')
    if preview.get("impact"):
        rows.append(f'<div class="confirmation-row">Impact: {escape(preview["impact"])}</div>')
    body = ""
    if preview.get("diff"):
        path = preview.get("diff_path")
        body = (f'<div class="path">{_ref_link(str(path), str(path))}</div>' if path else "") + diff_lines_html(str(preview["diff"]))
    elif preview.get("after"):
        body = f"<pre><code>{escape(preview['after'])}</code></pre>"
    note = "This cannot be undone." if irreversible else "The file is copied first, so /undo can put it back."
    css = "confirmation irreversible" if irreversible else "confirmation"
    return (
        f'<section class="{css}"><div class="confirmation-title">{escape(title)}</div>'
        f'<div class="confirmation-summary">{escape(summary)}</div>{"".join(rows)}{body}'
        f'<div class="confirmation-row">{escape(note)}</div>'
        + (f'<div class="confirmation-actions"><a class="approve" href="{CONFIRM_URL}">Approve</a><a class="cancel" href="{CANCEL_URL}">Cancel</a></div>' if actions else "")
        + "</section>"
    )


def render_page(body: str, *, title: str | None = None, theme: str = "light") -> str:
    heading = f'<h2 class="panel-title">{escape(title)}</h2>' if title else ""
    return (
        f'<!doctype html><html data-theme="{escape(theme)}"><head><meta charset="utf-8">'
        f"<style>{STYLE}</style></head><body>{heading}{body}</body></html>"
    )


__all__ = [
    "CANCEL_URL",
    "CONFIRM_URL",
    "MAX_TABLE_ROWS",
    "OPEN_SCHEME",
    "STYLE",
    "diff_lines_html",
    "escape",
    "open_url",
    "render_approval_bar",
    "render_body",
    "render_confirmation",
    "render_page",
    "render_result",
    "render_results",
]
