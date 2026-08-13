from __future__ import annotations

import re
from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.documents.search_service import DocumentSearchService
from core.state.search_result_context import SearchResultContext


class SearchCommandHandler:
    def __init__(self, search_service: DocumentSearchService, search_context: SearchResultContext | None = None, output: OutputSink | None = None) -> None:
        self.search_service = search_service
        self.search_context = search_context
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        if user_input.strip().lower() == "/search recent":
            results = self.search_service.recent(limit=5)
            if self.search_context is not None:
                self.search_context.update("recent", results)
            self._print_results(results)
            return True

        if user_input.startswith("/search "):
            query = user_input[len("/search "):].strip()
            if not query:
                emit_output(self.output, "Usage: /search <natural-language-query>")
                return True
            results = self.search_service.search(query, limit=5)
            if self.search_context is not None:
                self.search_context.update(query, results)
            self._print_results(results)
            return True

        if not user_input.startswith("/file "):
            return False

        parts = user_input.strip().split()
        if len(parts) < 3 or parts[1].lower() != "show":
            emit_output(self.output, "Usage: /file show <result-id>")
            return True

        record = self.search_service.get_record(parts[2])
        if record is None:
            emit_output(self.output, "Result not found.")
            return True

        emit_output(self.output, f"ID: {record.id}")
        emit_output(self.output, f"Path: {record.path}")
        emit_output(self.output, f"Name: {record.name}")
        emit_output(self.output, f"Extension: {record.extension}")
        emit_output(self.output, f"Modified: {record.modified_at}")
        emit_output(self.output, f"Status: {record.content_status}")
        if record.error:
            emit_output(self.output, f"Extractor error: {record.error}")
        return True

    def handle_natural_language(self, user_input: str) -> bool:
        if not self._looks_like_search_intent(user_input):
            return False
        results = self.search_service.search(user_input, limit=5)
        if self.search_context is not None:
            self.search_context.update(user_input, results)
        self._print_results(results)
        return True

    def _print_results(self, results: list[Any]) -> None:
        if not results:
            emit_output(self.output, "No matching files found.")
            return

        index = 1
        for item in results:
            record = item.record
            reasons = "; ".join(item.reasons)
            emit_output(self.output, f"{index}. {record.name}")
            emit_output(self.output, f"   id: {record.id}")
            emit_output(self.output, f"   path: {record.path}")
            emit_output(self.output, f"   modified: {record.modified_at}")
            emit_output(self.output, f"   match: {reasons}")
            index = index + 1

    def _looks_like_search_intent(self, user_input: str) -> bool:
        lowered = user_input.strip().lower()
        if not lowered:
            return False

        search_verbs = ["find", "looking for", "search for", "locate"]
        file_nouns = ["file", "document", "spreadsheet", "pdf", "word", "excel", "csv", "slides", "presentation"]

        has_verb = any(verb in lowered for verb in search_verbs)
        has_noun = any(noun in lowered for noun in file_nouns)
        has_date_hint = bool(re.search(r"last\s+month|last\s+\d+\s+days|today|yesterday", lowered))
        return has_noun and (has_verb or has_date_hint)

