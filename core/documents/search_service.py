from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.documents.catalog import DocumentCatalog
from core.documents.models import FileRecord, RankedSearchResult
from core.documents.query_parser import FileSearchQueryParser


class DocumentSearchService:
    def __init__(self, catalog: DocumentCatalog, query_parser: FileSearchQueryParser) -> None:
        self.catalog = catalog
        self.query_parser = query_parser

    def search(self, query_text: str, limit: int = 5) -> list[RankedSearchResult]:
        query = self.query_parser.parse(query_text)
        candidates = self.catalog.search_candidates(query, limit=500)
        scored = [self._score_record(record, query.text_terms, query.extensions) for record in candidates]
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]

    def recent(self, limit: int = 5) -> list[RankedSearchResult]:
        records = self.catalog.recent_documents(limit=limit)
        return [self._score_record(record, [], []) for record in records]

    def get_record(self, record_id: str) -> FileRecord | None:
        return self.catalog.get_by_id(record_id)

    def _score_record(self, record: FileRecord, terms: list[str], query_extensions: list[str]) -> RankedSearchResult:
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

        modified = self._parse_datetime(record.modified_at)
        if modified is not None and modified >= datetime.now(timezone.utc) - timedelta(days=30):
            score = score + 5.0
            reasons.append("recently modified")

        if not reasons:
            reasons.append("metadata proximity match")
        return RankedSearchResult(record=record, score=score, reasons=reasons)

    def _parse_datetime(self, value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

