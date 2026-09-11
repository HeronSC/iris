# File: core/web/__init__.py

from __future__ import annotations

from core.web.fetch import FetchedPage, PageFetcher, UrlRejected, check_public_url

__all__ = ["FetchedPage", "PageFetcher", "UrlRejected", "check_public_url"]
