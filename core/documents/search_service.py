# File: core/documents/search_service.py

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.documents.catalog import DocumentCatalog
from core.documents.models import FileRecord, RankedSearchResult
from core.documents.query_parser import FileSearchQueryParser

logger = logging.getLogger(__name__)

_SEMANTIC_WEIGHT = 40.0


class DocumentSearchService:
    def __init__(self, catalog: DocumentCatalog, query_parser: FileSearchQueryParser, embeddings: Any | None = None) -> None:
        self.catalog = catalog
        self.query_parser = query_parser
        self.embeddings = embeddings

    def search(self, query_text: str, limit: int = 5) -> list[RankedSearchResult]:
        query = self.query_parser.parse(query_text)
        candidates = self.catalog.search_candidates(query, limit=500)
        hits = self._semantic_hits(query_text) if query.text_terms else {}
        if hits:
            known = {record.path for record in candidates}
            for path in hits:
                if path in known:
                    continue
                record = self.catalog.get_by_path(path)
                if record is not None:
                    candidates.append(record)
        scored = [self._score_record(record, query.text_terms, query.extensions, hits.get(record.path)) for record in candidates]
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]

    def recent(self, limit: int = 5) -> list[RankedSearchResult]:
        records = self.catalog.recent_documents(limit=limit)
        return [self._score_record(record, [], []) for record in records]

    def get_record(self, record_id: str) -> FileRecord | None:
        return self.catalog.get_by_id(record_id)

    def _semantic_hits(self, query_text: str) -> dict[str, Any]:
        index = self.embeddings
        if index is None or not getattr(index, "available", False):
            return {}
        try:
            return {hit.path: hit for hit in index.search(query_text, k=50)}
        except Exception as error:
            logger.warning("Semantic document search unavailable: %s", error)
            return {}

    def _score_record(self, record: FileRecord, terms: list[str], query_extensions: list[str], hit: Any | None = None) -> RankedSearchResult:
        score = 0.0
        reasons: list[str] = []
        name_lower = record.name.lower()
        path_lower = record.path.lower()
        text_lower = record.extracted_text.lower()

        if query_extensions and record.extension in query_extensions:
            score = score + 15.0
            reasons.append(f"extension matches {record.extension}")

        matched_terms: set[str] = set()
        for term in terms:
            if term in name_lower:
                score = score + 25.0
                reasons.append(f"filename contains '{term}'")
                matched_terms.add(term)
            elif term in path_lower:
                score = score + 12.0
                reasons.append(f"path contains '{term}'")
                matched_terms.add(term)
            elif term in text_lower:
                score = score + 8.0
                reasons.append(f"content contains '{term}'")
                matched_terms.add(term)

        if matched_terms:
            score = score + (10.0 * len(matched_terms))
            reasons.append(f"matched {len(matched_terms)} query term(s)")
            if len(matched_terms) == len(set(terms)) and terms:
                score = score + 20.0
                reasons.append("matches all query terms")

        snippet: str | None = None
        location: str | None = None
        if hit is not None and self.embeddings is not None:
            strength = self.embeddings.config.relevance(hit.similarity)
            if strength > 0.0:
                score = score + _SEMANTIC_WEIGHT * strength
                where = f", page {hit.page}" if hit.page else ""
                reasons.append(f"a passage matches by meaning ({hit.similarity:.2f}{where})")
                snippet = " ".join(record.extracted_text[hit.start : hit.end].split())[:240] or None
                location = f"page {hit.page}" if hit.page else f"chars {hit.start}-{hit.end}"

        modified = self._parse_datetime(record.modified_at)
        if modified is not None and modified >= datetime.now(timezone.utc) - timedelta(days=30):
            score = score + 5.0
            reasons.append("recently modified")

        if not reasons:
            reasons.append("metadata proximity match")
        return RankedSearchResult(record=record, score=score, reasons=reasons, snippet=snippet, location=location)

    def _parse_datetime(self, value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
