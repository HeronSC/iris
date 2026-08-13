from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.documents.models import FileSearchQuery


@dataclass(frozen=True)
class QueryParserConfig:
    default_roots: list[Path]


class FileSearchQueryParser:
    def __init__(self, config: QueryParserConfig) -> None:
        self.config = config

    def parse(self, query_text: str) -> FileSearchQuery:
        lowered = query_text.lower()
        extensions = self._extract_extensions(lowered)
        type_tokens = self._recognized_type_tokens(lowered)
        created_after, created_before = self._extract_date_range(lowered)
        terms = self._extract_terms(query_text, excluded_terms=type_tokens)
        return FileSearchQuery(
            text_terms=terms,
            extensions=extensions,
            created_after=created_after,
            created_before=created_before,
            modified_after=None,
            modified_before=None,
            locations=self.config.default_roots,
        )

    def _extract_extensions(self, lowered: str) -> list[str]:
        explicit_extensions = re.findall(r"\.[a-z0-9]{2,5}", lowered)
        if explicit_extensions:
            return sorted(set(explicit_extensions))

        mapped: set[str] = set()
        if "excel" in lowered or "spreadsheet" in lowered:
            mapped.add(".xlsx")
            mapped.add(".xls")
        if "pdf" in lowered:
            mapped.add(".pdf")
        if "word" in lowered or "doc" in lowered:
            mapped.add(".docx")
        if "text" in lowered or "notes" in lowered:
            mapped.add(".txt")
            mapped.add(".md")
        if "csv" in lowered:
            mapped.add(".csv")
        if "powerpoint" in lowered or "slides" in lowered:
            mapped.add(".pptx")
        return sorted(mapped)

    def _recognized_type_tokens(self, lowered: str) -> set[str]:
        tokens: set[str] = set()
        if "excel" in lowered:
            tokens.add("excel")
        if "spreadsheet" in lowered:
            tokens.add("spreadsheet")
        if "pdf" in lowered:
            tokens.add("pdf")
        if "word" in lowered:
            tokens.add("word")
        if "doc" in lowered:
            tokens.add("doc")
        if "csv" in lowered:
            tokens.add("csv")
        if "powerpoint" in lowered:
            tokens.add("powerpoint")
        if "slides" in lowered:
            tokens.add("slides")
        return tokens

    def _extract_date_range(self, lowered: str) -> tuple[datetime | None, datetime | None]:
        now = datetime.now(timezone.utc)
        if "last month" in lowered:
            month = now.month - 1
            year = now.year
            if month == 0:
                month = 12
                year = year - 1
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            if month == 12:
                end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
            else:
                end = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
            return start, end

        recent_days = re.search(r"last\s+(\d+)\s+days", lowered)
        if recent_days is not None:
            day_count = int(recent_days.group(1))
            return now - timedelta(days=day_count), now

        if "today" in lowered:
            start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            return start, now

        if "yesterday" in lowered:
            start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=1)
            end = start + timedelta(days=1) - timedelta(seconds=1)
            return start, end

        return None, None

    def _extract_terms(self, query_text: str, excluded_terms: set[str] | None = None) -> list[str]:
        normalized = re.sub(r"[^a-zA-Z0-9*\-\s]", " ", query_text)
        raw_terms = [term.strip().lower() for term in normalized.split() if term.strip()]
        excluded = excluded_terms or set()
        stop_words = {
            "i",
            "am",
            "looking",
            "for",
            "file",
            "files",
            "document",
            "documents",
            "related",
            "to",
            "the",
            "a",
            "an",
            "created",
            "last",
            "month",
            "days",
            "think",
            "it",
            "was",
            "my",
        }
        terms = [
            term.replace("*", "").replace("-", "")
            for term in raw_terms
            if term not in stop_words and term not in excluded and len(term) > 1
        ]
        unique_terms: list[str] = []
        seen: set[str] = set()
        for term in terms:
            if term not in seen:
                seen.add(term)
                unique_terms.append(term)
        return unique_terms

