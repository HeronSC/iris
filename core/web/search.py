# File: core/web/search.py

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_URL = "http://127.0.0.1:8888"
DEFAULT_USER_AGENT = "Iris/1.0 (local desktop assistant)"

CATEGORIES: tuple[str, ...] = ("general", "images", "videos", "news")
TIME_RANGES: tuple[str, ...] = ("day", "week", "month", "year")

START_HINT = "Search is not running. Start it with: docker compose -f docker/docker-compose.yml up -d (see docker/README.md)."


class SearchUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    engines: tuple[str, ...] = ()
    category: str = "general"
    published: str | None = None
    thumbnail: str | None = None
    image_url: str | None = None
    score: float = 0.0

    @classmethod
    def from_json(cls, payload: dict[str, Any], category: str) -> "SearchHit | None":
        url = str(payload.get("url") or "").strip()
        if not url:
            return None
        engines = payload.get("engines")
        if not isinstance(engines, list):
            engines = [payload.get("engine")] if payload.get("engine") else []
        published = payload.get("publishedDate") or payload.get("published_date")
        thumbnail = payload.get("thumbnail") or payload.get("thumbnail_src") or None
        image_url = payload.get("img_src") or None
        try:
            score = float(payload.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        return cls(
            title=" ".join(str(payload.get("title") or url).split()),
            url=url,
            snippet=" ".join(str(payload.get("content") or "").split()),
            engines=tuple(str(item) for item in engines if item),
            category=str(payload.get("category") or category),
            published=str(published)[:10] if published else None,
            thumbnail=str(thumbnail) if thumbnail else None,
            image_url=str(image_url) if image_url else None,
            score=score,
        )


@dataclass(frozen=True)
class SearchResponse:
    query: str
    category: str
    hits: tuple[SearchHit, ...]
    suggestions: tuple[str, ...] = ()
    unresponsive: tuple[str, ...] = ()
    searched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    from_cache: bool = False

    @property
    def source_line(self) -> str:
        cached = ", cached" if self.from_cache else ""
        return f"Source: SearXNG {self.category} search for \"{self.query}\" ({self.searched_at[:16].replace('T', ' ')} UTC{cached})"


class SearchClient:
    def __init__(
        self,
        base_url: str = DEFAULT_SEARCH_URL,
        *,
        timeout_seconds: float = 12.0,
        cache_ttl_seconds: float = 300.0,
        language: str = "en-US",
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self.language = language
        self.user_agent = user_agent
        self.transport = transport
        self._cache: dict[tuple[str, str, str, int], tuple[float, SearchResponse]] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            with httpx.Client(timeout=min(self.timeout_seconds, 3.0), headers={"User-Agent": self.user_agent}, transport=self.transport) as client:
                response = client.get(f"{self.base_url}/healthz")
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    def search(
        self,
        query: str,
        *,
        category: str = "general",
        time_range: str | None = None,
        page: int = 1,
        max_results: int = 10,
    ) -> SearchResponse:
        text = " ".join((query or "").split())
        if not text:
            raise ValueError("No search query given")
        if category not in CATEGORIES:
            raise ValueError(f"Unknown search category {category!r}; one of {', '.join(CATEGORIES)}")
        if time_range is not None and time_range not in TIME_RANGES:
            raise ValueError(f"Unknown time range {time_range!r}; one of {', '.join(TIME_RANGES)}")
        key = (text.lower(), category, time_range or "", page)
        cached = self._cached(key)
        if cached is not None:
            return self._trim(cached, max_results)
        params: dict[str, Any] = {"q": text, "format": "json", "categories": category, "language": self.language, "pageno": page}
        if time_range:
            params["time_range"] = time_range
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout_seconds, headers=headers, transport=self.transport) as client:
                response = client.get(f"{self.base_url}/search", params=params)
        except httpx.ConnectError as error:
            raise SearchUnavailable(START_HINT) from error
        except httpx.HTTPError as error:
            raise SearchUnavailable(f"Search did not answer: {error}") from error
        if response.status_code == 403:
            raise SearchUnavailable("Search refused the JSON request; docker/searxng/settings.yml must list json under search.formats.")
        if response.status_code >= 400:
            raise SearchUnavailable(f"Search answered {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise SearchUnavailable("Search returned something other than JSON; check search.formats in docker/searxng/settings.yml.") from error
        result = self._parse(text, category, payload)
        self._remember(key, result)
        return self._trim(result, max_results)

    def _parse(self, query: str, category: str, payload: Any) -> SearchResponse:
        if not isinstance(payload, dict):
            raise SearchUnavailable("Search returned an unexpected shape")
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            hit = SearchHit.from_json(item, category)
            if hit is None or hit.url in seen:
                continue
            seen.add(hit.url)
            hits.append(hit)
        hits.sort(key=lambda hit: hit.score, reverse=True)
        unresponsive = tuple(
            f"{entry[0]} ({entry[1]})" if isinstance(entry, (list, tuple)) and len(entry) > 1 else str(entry)
            for entry in payload.get("unresponsive_engines") or []
        )
        suggestions = tuple(str(item) for item in payload.get("suggestions") or [] if str(item).strip())
        return SearchResponse(query=query, category=category, hits=tuple(hits), suggestions=suggestions[:5], unresponsive=unresponsive)

    @staticmethod
    def _trim(response: SearchResponse, max_results: int) -> SearchResponse:
        limit = max(1, int(max_results))
        if len(response.hits) <= limit:
            return response
        return SearchResponse(
            query=response.query,
            category=response.category,
            hits=response.hits[:limit],
            suggestions=response.suggestions,
            unresponsive=response.unresponsive,
            searched_at=response.searched_at,
            from_cache=response.from_cache,
        )

    def _cached(self, key: tuple[str, str, str, int]) -> SearchResponse | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            stored_at, response = entry
            if time.monotonic() - stored_at > self.cache_ttl_seconds:
                del self._cache[key]
                return None
        return SearchResponse(**{**response.__dict__, "from_cache": True})

    def _remember(self, key: tuple[str, str, str, int], response: SearchResponse) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic(), response)
            if len(self._cache) > 100:
                oldest = min(self._cache.items(), key=lambda item: item[1][0])[0]
                del self._cache[oldest]


__all__ = [
    "CATEGORIES",
    "DEFAULT_SEARCH_URL",
    "START_HINT",
    "TIME_RANGES",
    "SearchClient",
    "SearchHit",
    "SearchResponse",
    "SearchUnavailable",
]
