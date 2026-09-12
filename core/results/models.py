# File: core/results/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Sequence
from uuid import uuid4


class ResultKind(str, Enum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"
    VIDEO = "video"
    FILE = "file"
    CODE = "code"
    DIFF = "diff"
    LINK = "link"
    CHART = "chart"
    STATUS = "status"


class ResultError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Source:
    name: str
    kind: str = "tool"
    ref: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.ref:
            payload["ref"] = self.ref
        return payload

    @classmethod
    def from_json(cls, payload: Any) -> "Source":
        if isinstance(payload, str):
            return cls(name=payload)
        if not isinstance(payload, dict) or not str(payload.get("name", "")).strip():
            raise ResultError("A result needs a source with a name")
        return cls(name=str(payload["name"]), kind=str(payload.get("kind") or "tool"), ref=payload.get("ref") or None)


REQUIRED_FIELDS: dict[ResultKind, tuple[str, ...]] = {
    ResultKind.TEXT: ("text",),
    ResultKind.TABLE: ("columns", "rows"),
    ResultKind.IMAGE: ("mime_type",),
    ResultKind.VIDEO: ("uri",),
    ResultKind.FILE: ("path",),
    ResultKind.CODE: ("code",),
    ResultKind.DIFF: ("unified",),
    ResultKind.LINK: ("url",),
    ResultKind.CHART: ("chart_type", "series"),
    ResultKind.STATUS: ("state", "message"),
}

STATUS_STATES = ("ok", "warning", "error", "pending")


@dataclass(frozen=True)
class Result:
    kind: ResultKind
    source: Source
    data: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        missing = [name for name in REQUIRED_FIELDS[self.kind] if name not in self.data]
        if missing:
            raise ResultError(f"A {self.kind.value} result needs {', '.join(missing)}")
        if self.kind is ResultKind.IMAGE and not (self.data.get("uri") or self.data.get("base64")):
            raise ResultError("An image result needs a uri or base64 data")
        if self.kind is ResultKind.STATUS and self.data["state"] not in STATUS_STATES:
            raise ResultError(f"A status result's state is one of {', '.join(STATUS_STATES)}")
        if self.kind is ResultKind.TABLE:
            width = len(self.data["columns"])
            for row in self.data["rows"]:
                if len(row) != width:
                    raise ResultError(f"A table row has {len(row)} cells but there are {width} columns")

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind.value,
            "source": self.source.to_json(),
            "created_at": self.created_at,
            "data": dict(self.data),
        }
        if self.title:
            payload["title"] = self.title
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Result":
        try:
            kind = ResultKind(str(payload.get("kind", "")))
        except ValueError as error:
            raise ResultError(f"Unknown result kind: {payload.get('kind')!r}") from error
        data = payload.get("data")
        return cls(
            kind=kind,
            source=Source.from_json(payload.get("source")),
            data=dict(data) if isinstance(data, dict) else {},
            title=payload.get("title") or None,
            id=str(payload.get("id") or uuid4().hex[:12]),
            created_at=str(payload.get("created_at") or utc_now_iso()),
        )


def _source(source: Source | str, kind: str, ref: str | None) -> Source:
    if isinstance(source, Source):
        return source
    return Source(name=str(source), kind=kind, ref=ref)


def text(content: str, *, source: Source | str, title: str | None = None, ref: str | None = None, kind: str = "tool", format: str = "markdown") -> Result:
    return Result(ResultKind.TEXT, _source(source, kind, ref), {"text": str(content), "format": format}, title=title)


def table(columns: Sequence[str], rows: Iterable[Sequence[Any]], *, source: Source | str, title: str | None = None, ref: str | None = None, kind: str = "tool") -> Result:
    return Result(
        ResultKind.TABLE,
        _source(source, kind, ref),
        {"columns": [str(item) for item in columns], "rows": [list(row) for row in rows]},
        title=title,
    )


def image(*, mime_type: str, source: Source | str, uri: str | None = None, base64: str | None = None, alt: str | None = None, title: str | None = None, ref: str | None = None, kind: str = "tool") -> Result:
    data: dict[str, Any] = {"mime_type": mime_type}
    if uri:
        data["uri"] = uri
    if base64:
        data["base64"] = base64
    if alt:
        data["alt"] = alt
    return Result(ResultKind.IMAGE, _source(source, kind, ref), data, title=title)


def video(uri: str, *, source: Source | str, mime_type: str | None = None, title: str | None = None, ref: str | None = None, kind: str = "tool") -> Result:
    data: dict[str, Any] = {"uri": uri}
    if mime_type:
        data["mime_type"] = mime_type
    return Result(ResultKind.VIDEO, _source(source, kind, ref), data, title=title)


def file(path: str, *, source: Source | str, name: str | None = None, size_bytes: int | None = None, mime_type: str | None = None, title: str | None = None, ref: str | None = None, kind: str = "tool") -> Result:
    data: dict[str, Any] = {"path": path, "name": name or path.replace("\\", "/").rsplit("/", 1)[-1]}
    if size_bytes is not None:
        data["size_bytes"] = int(size_bytes)
    if mime_type:
        data["mime_type"] = mime_type
    return Result(ResultKind.FILE, _source(source, kind, ref), data, title=title)


def code(content: str, *, source: Source | str, language: str | None = None, path: str | None = None, title: str | None = None, ref: str | None = None, kind: str = "tool") -> Result:
    data: dict[str, Any] = {"code": content, "language": language or ""}
    if path:
        data["path"] = path
    return Result(ResultKind.CODE, _source(source, kind, ref), data, title=title)


def diff(unified: str, *, source: Source | str, path: str | None = None, title: str | None = None, ref: str | None = None, kind: str = "tool") -> Result:
    data: dict[str, Any] = {"unified": unified}
    if path:
        data["path"] = path
    return Result(ResultKind.DIFF, _source(source, kind, ref), data, title=title)


def link(url: str, *, source: Source | str, title: str | None = None, description: str | None = None, author: str | None = None, date: str | None = None, ref: str | None = None, kind: str = "tool") -> Result:
    data: dict[str, Any] = {"url": url}
    for name, value in (("description", description), ("author", author), ("date", date)):
        if value:
            data[name] = value
    return Result(ResultKind.LINK, _source(source, kind, ref or url), data, title=title)


def chart(chart_type: str, series: Sequence[dict[str, Any]], *, source: Source | str, labels: Sequence[Any] | None = None, title: str | None = None, ref: str | None = None, kind: str = "tool", units: str | None = None) -> Result:
    data: dict[str, Any] = {"chart_type": chart_type, "series": [dict(item) for item in series]}
    if labels is not None:
        data["labels"] = list(labels)
    if units:
        data["units"] = units
    return Result(ResultKind.CHART, _source(source, kind, ref), data, title=title)


def status(state: str, message: str, *, source: Source | str, details: dict[str, Any] | None = None, title: str | None = None, ref: str | None = None, kind: str = "tool") -> Result:
    data: dict[str, Any] = {"state": state, "message": message}
    if details:
        data["details"] = dict(details)
    return Result(ResultKind.STATUS, _source(source, kind, ref), data, title=title)


def to_json_list(results: Iterable[Result]) -> list[dict[str, Any]]:
    return [item.to_json() for item in results]


def from_json_list(payload: Any) -> tuple[Result, ...]:
    if not isinstance(payload, list):
        return ()
    found: list[Result] = []
    for item in payload:
        if isinstance(item, dict):
            try:
                found.append(Result.from_json(item))
            except ResultError:
                continue
    return tuple(found)


__all__ = [
    "REQUIRED_FIELDS",
    "Result",
    "ResultError",
    "ResultKind",
    "STATUS_STATES",
    "Source",
    "chart",
    "code",
    "diff",
    "file",
    "from_json_list",
    "image",
    "link",
    "status",
    "table",
    "text",
    "to_json_list",
    "utc_now_iso",
    "video",
]
