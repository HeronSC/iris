# File: core/knowledge/export.py

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.knowledge.models import MemoryRecord

STAMP = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True)
class ExportReport:
    markdown_path: Path
    json_path: Path
    records: int
    topics: int

    @property
    def summary(self) -> str:
        return f"Exported {self.records} record(s) across {self.topics} topic(s) to {self.markdown_path.name} and {self.json_path.name}"


def _line(record: MemoryRecord) -> str:
    when = record.occurred_at or record.created_at
    status = record.status.value if record.status else ""
    head = f"- **{record.kind.value}** ({status}, {when[:10]}, {record.source})"
    content = record.content.strip().replace("\n", "\n  ")
    text = f"{head}\n  {content}"
    if record.confidence is not None:
        text += f"\n  confidence: {record.confidence:.2f}"
    if record.data:
        compact = json.dumps(record.data, ensure_ascii=False, sort_keys=True)
        if len(compact) <= 200:
            text += f"\n  data: `{compact}`"
    text += f"\n  id: `{record.id}`"
    return text


def render_markdown(records: list[MemoryRecord], *, title: str = "Iris memory") -> str:
    by_topic: dict[str, list[MemoryRecord]] = {}
    for record in records:
        by_topic.setdefault(record.topic, []).append(record)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = [f"# {title}", "", f"Exported {stamp}. {len(records)} record(s), {len(by_topic)} topic(s). Superseded records are left out.", ""]
    for topic in sorted(by_topic):
        lines.append(f"## {topic}")
        lines.append("")
        for record in by_topic[topic]:
            lines.append(_line(record))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_memory(records: list[MemoryRecord], folder: str | Path, *, links: list[dict[str, Any]] | None = None, title: str = "Iris memory") -> ExportReport:
    destination = Path(folder).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime(STAMP)
    markdown_path = destination / f"knowledge-{stamp}.md"
    json_path = destination / f"knowledge-{stamp}.json"
    markdown_path.write_text(render_markdown(records, title=title), encoding="utf-8")
    payload = {
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "records": [_record_json(record) for record in records],
        "links": list(links or []),
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ExportReport(markdown_path=markdown_path, json_path=json_path, records=len(records), topics=len({record.topic for record in records}))


def _record_json(record: MemoryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind.value,
        "topic": record.topic,
        "status": record.status.value if record.status else None,
        "content": record.content,
        "data": dict(record.data),
        "confidence": record.confidence,
        "source": record.source,
        "source_ref": record.source_ref,
        "occurred_at": record.occurred_at,
        "created_at": record.created_at,
        "supersedes": getattr(record, "supersedes", None),
        "superseded_by": getattr(record, "superseded_by", None),
    }


__all__ = ["ExportReport", "export_memory", "render_markdown"]
