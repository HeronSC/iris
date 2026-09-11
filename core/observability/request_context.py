# File: core/observability/request_context.py

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from uuid import uuid4

import structlog.contextvars as _ctx

REQUEST_ID_KEY = "request_id"


def new_request_id() -> str:
    return uuid4().hex[:12]


def bind_request(request_id: str | None = None, **extra: object) -> str:
    resolved = request_id or new_request_id()
    _ctx.bind_contextvars(**{REQUEST_ID_KEY: resolved}, **extra)
    return resolved


def clear_request() -> None:
    _ctx.clear_contextvars()


def current_request_id() -> str | None:
    value = _ctx.get_contextvars().get(REQUEST_ID_KEY)
    return str(value) if value else None


@contextmanager
def request_scope(request_id: str | None = None, **extra: object) -> Iterator[str]:
    resolved = bind_request(request_id, **extra)
    try:
        yield resolved
    finally:
        clear_request()
