# File: core/web/fetch.py

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Iris/1.0 (local desktop assistant)"


class UrlRejected(ValueError):
    pass


def check_public_url(url: str, *, resolve: Callable[[str], list[str]] | None = None) -> str:
    candidate = (url or "").strip()
    if not candidate:
        raise UrlRejected("No URL given")
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise UrlRejected(f"Only http and https URLs can be fetched, not {parsed.scheme or 'this'}")
    host = parsed.hostname
    if not host:
        raise UrlRejected("The URL has no host")
    if parsed.username or parsed.password:
        raise UrlRejected("URLs with credentials are not fetched")
    if host.lower() in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise UrlRejected(f"{host} is a local address; local services are not reached through page fetches")
    addresses: list[str]
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        resolver = resolve or _resolve
        try:
            addresses = resolver(host)
        except OSError as error:
            raise UrlRejected(f"{host} could not be resolved: {error}") from error
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise UrlRejected(f"{host} points at a private or local address ({address}); not fetched")
    return candidate


def _resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    title: str | None
    author: str | None
    date: str | None
    text: str
    fetched_at: str
    from_cache: bool = False
    content_type: str = ""

    @property
    def source_line(self) -> str:
        cached = ", cached" if self.from_cache else ""
        return f"Source: {self.final_url} (fetched {self.fetched_at[:16].replace('T', ' ')} UTC{cached})"


class PageFetcher:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 15.0,
        max_bytes: int = 3_000_000,
        cache_ttl_seconds: float = 600.0,
        per_host_interval_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        resolve: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.cache_ttl_seconds = cache_ttl_seconds
        self.per_host_interval_seconds = per_host_interval_seconds
        self.transport = transport
        self.resolve = resolve
        self._cache: dict[str, tuple[float, FetchedPage]] = {}
        self._last_request_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_url(self, url: str) -> str:
        return check_public_url(url, resolve=self.resolve)

    def fetch(self, url: str) -> FetchedPage:
        target = self.check_url(url)
        cached = self._cached(target)
        if cached is not None:
            return cached
        self._throttle(urlparse(target).hostname or "")
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"}
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, headers=headers, transport=self.transport) as client:
            with client.stream("GET", target) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > self.max_bytes:
                        raise ValueError(f"Page is larger than {self.max_bytes // 1_000_000} MB; not fetched")
                    chunks.append(chunk)
                body = b"".join(chunks)
                final_url = str(response.url)
                status_code = response.status_code
                encoding = response.encoding or "utf-8"
        html = body.decode(encoding, errors="replace")
        page = self._extract(target, final_url, status_code, content_type, html)
        self._remember(target, page)
        return page


    def _extract(self, url: str, final_url: str, status_code: int, content_type: str, html: str) -> FetchedPage:
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        title = author = date = None
        text = ""
        if "html" in content_type or "<html" in html[:2000].lower():
            try:
                #! @allow-local-import
                import trafilatura

                raw = trafilatura.extract(
                    html,
                    url=final_url,
                    output_format="json",
                    with_metadata=True,
                    include_comments=False,
                    include_tables=True,
                    date_extraction_params={"extensive_search": False, "original_date": True},
                )
                if raw:
                    payload: dict[str, Any] = json.loads(raw)
                    title = payload.get("title") or None
                    author = payload.get("author") or None
                    date = payload.get("date") or None
                    text = str(payload.get("text") or "").strip()
            except Exception as error:
                logger.warning("Extraction failed for %s: %s", final_url, error)
            if not title:
                title = _title_from_html(html)
        if not text and content_type.startswith("text/") and "html" not in content_type:
            text = html.strip()
        return FetchedPage(
            url=url,
            final_url=final_url,
            status_code=status_code,
            title=title,
            author=author,
            date=date,
            text=text,
            fetched_at=fetched_at,
            content_type=content_type,
        )

    def _cached(self, url: str) -> FetchedPage | None:
        with self._lock:
            entry = self._cache.get(url)
            if entry is None:
                return None
            stored_at, page = entry
            if time.monotonic() - stored_at > self.cache_ttl_seconds:
                del self._cache[url]
                return None
        return FetchedPage(**{**page.__dict__, "from_cache": True})

    def _remember(self, url: str, page: FetchedPage) -> None:
        with self._lock:
            self._cache[url] = (time.monotonic(), page)
            if len(self._cache) > 200:
                oldest = min(self._cache.items(), key=lambda item: item[1][0])[0]
                del self._cache[oldest]

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_request_at.get(host)
            now = time.monotonic()
            wait = 0.0 if last is None else self.per_host_interval_seconds - (now - last)
            self._last_request_at[host] = max(now, (last or 0.0) + self.per_host_interval_seconds)
        if wait > 0:
            time.sleep(wait)


def _title_from_html(html: str) -> str | None:
    lowered = html.lower()
    start = lowered.find("<title")
    if start < 0:
        return None
    start = lowered.find(">", start)
    end = lowered.find("</title>", start)
    if start < 0 or end < 0:
        return None
    title = " ".join(html[start + 1 : end].split())
    return title or None
