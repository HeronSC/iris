# File: core/actions/diffs.py

from __future__ import annotations

import difflib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.actions.models import ConfirmationPreview

MAX_DIFF_LINES = 120


def render_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def unified_file_diff(path: Path, after_text: str, *, max_lines: int = MAX_DIFF_LINES) -> str:
    try:
        before_text = path.read_text(encoding="utf-8-sig")
    except OSError:
        before_text = ""
    lines = list(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile=f"{path.name} (now)",
            tofile=f"{path.name} (after)",
            lineterm="",
            n=2,
        )
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... {len(lines) - max_lines} more line(s)"]
    return "\n".join(lines)


def unified_text_diff(before_text: str, after_text: str, *, label: str, max_lines: int = MAX_DIFF_LINES) -> str:
    lines = list(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile=f"{label} (now)",
            tofile=f"{label} (after)",
            lineterm="",
            n=2,
        )
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... {len(lines) - max_lines} more line(s)"]
    return "\n".join(lines)


def with_section_diff(preview: ConfirmationPreview, config: dict[str, Any]) -> ConfirmationPreview:
    try:
        after_payload = json.loads(preview.after or "{}")
    except ValueError:
        return preview
    if not isinstance(after_payload, dict):
        return preview
    before_payload = {key: config.get(key) for key in after_payload}
    metadata = dict(preview.metadata)
    metadata["diff"] = unified_text_diff(render_json(before_payload), render_json(after_payload), label=preview.target or "config.json")
    return replace(preview, metadata=metadata)


def with_file_diff(preview: ConfirmationPreview, path: Path, after_payload: Any) -> ConfirmationPreview:
    diff = unified_file_diff(path, render_json(after_payload))
    metadata = dict(preview.metadata)
    metadata["diff"] = diff
    metadata["diff_path"] = str(path)
    return replace(preview, metadata=metadata)


__all__ = ["MAX_DIFF_LINES", "render_json", "unified_file_diff", "unified_text_diff", "with_file_diff", "with_section_diff"]
