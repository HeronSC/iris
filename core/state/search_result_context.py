from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.documents.models import RankedSearchResult


@dataclass(frozen=True)
class SearchResultRef:
    rank: int
    file_id: str
    name: str
    path: str


class SearchResultContext:
    def __init__(self, ttl_minutes: int = 30) -> None:
        self.ttl_minutes = ttl_minutes
        self._query: str | None = None
        self._created_at: datetime | None = None
        self._results: list[SearchResultRef] = []

    def update(self, query: str, results: list[RankedSearchResult]) -> None:
        self._query = query
        self._created_at = datetime.now(timezone.utc)
        mapped: list[SearchResultRef] = []
        rank = 1
        for item in results:
            mapped.append(
                SearchResultRef(
                    rank=rank,
                    file_id=item.record.id,
                    name=item.record.name,
                    path=item.record.path,
                )
            )
            rank = rank + 1
        self._results = mapped

    def get(self, number: int) -> SearchResultRef | None:
        if number <= 0:
            return None
        if self._created_at is None:
            return None
        if datetime.now(timezone.utc) - self._created_at > timedelta(minutes=self.ttl_minutes):
            return None
        for item in self._results:
            if item.rank == number:
                return item
        return None

    def has_recent_results(self) -> bool:
        return self.get(1) is not None

