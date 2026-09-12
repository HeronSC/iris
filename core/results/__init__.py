# File: core/results/__init__.py

from __future__ import annotations

from core.results.models import (
    Result,
    ResultError,
    ResultKind,
    Source,
    chart,
    code,
    diff,
    file,
    from_json_list,
    image,
    link,
    status,
    table,
    text,
    to_json_list,
    video,
)
from core.results.render import render_all, render_markdown, render_plain, to_detail, to_memory_record

__all__ = [
    "Result",
    "ResultError",
    "ResultKind",
    "Source",
    "chart",
    "code",
    "diff",
    "file",
    "from_json_list",
    "image",
    "link",
    "render_all",
    "render_markdown",
    "render_plain",
    "status",
    "table",
    "text",
    "to_detail",
    "to_json_list",
    "to_memory_record",
    "video",
]
