# File: core/permissions/limits.py

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class RateLimit:
    limit: int
    per_seconds: float

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("A rate limit cannot be negative")
        if self.per_seconds <= 0:
            raise ValueError("A rate limit needs a positive window")

    @classmethod
    def from_json(cls, payload: Any) -> "RateLimit":
        if isinstance(payload, dict):
            return cls(limit=int(payload.get("limit", 0)), per_seconds=float(payload.get("per_seconds", 3600.0)))
        raise ValueError(f"A rate limit must be an object, got {type(payload).__name__}")

    def describe(self) -> str:
        if self.per_seconds >= 3600 and self.per_seconds % 3600 == 0:
            window = f"{int(self.per_seconds // 3600)}h"
        elif self.per_seconds >= 60 and self.per_seconds % 60 == 0:
            window = f"{int(self.per_seconds // 60)}m"
        else:
            window = f"{self.per_seconds:g}s"
        return f"{self.limit} per {window}"


class RateLimiter:
    def __init__(self, limits: dict[str, RateLimit] | None = None, clock: Callable[[], float] | None = None) -> None:
        self.limits = dict(limits or {})
        self.clock = clock or time.monotonic
        self._used: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def limit_for(self, bucket: str) -> RateLimit | None:
        return self.limits.get(bucket)

    def remaining(self, bucket: str) -> int | None:
        limit = self.limits.get(bucket)
        if limit is None:
            return None
        with self._lock:
            return max(0, limit.limit - len(self._trim(bucket, limit)))

    def consume(self, bucket: str, amount: int = 1) -> bool:
        limit = self.limits.get(bucket)
        if limit is None:
            return True
        with self._lock:
            used = self._trim(bucket, limit)
            if len(used) + amount > limit.limit:
                return False
            now = self.clock()
            for _ in range(amount):
                used.append(now)
            return True

    def _trim(self, bucket: str, limit: RateLimit) -> deque[float]:
        used = self._used.setdefault(bucket, deque())
        cutoff = self.clock() - limit.per_seconds
        while used and used[0] <= cutoff:
            used.popleft()
        return used


__all__ = ["RateLimit", "RateLimiter"]
