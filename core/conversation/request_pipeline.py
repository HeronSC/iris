# File: core/conversation/request_pipeline.py

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from core.conversation.session import ConversationSession
from core.documents.models import DocumentSearchRoot


@dataclass(frozen=True)
class RequestPipelineResult:
    intent: str
    requires_tool: bool
    response_allowed: bool
    target: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    needs_clarification: bool = False


class RequestPipeline:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    def build_request(self, user_message: str, state: dict[str, Any] | None = None) -> RequestPipelineResult:
        text = (user_message or "").strip()
        if not text:
            return RequestPipelineResult(intent="none", requires_tool=False, response_allowed=True)

        session = state.get("session") if isinstance(state, dict) else None
        if isinstance(session, ConversationSession):
            pending = session.pending_interaction
            if pending is not None and self._looks_like_selection(text):
                selection = self._extract_selection(text)
                if selection is not None:
                    return RequestPipelineResult(
                        intent="select_pending_result",
                        requires_tool=True,
                        response_allowed=False,
                        target={"type": "selection", "selection": selection},
                        scope={"root_alias": session.active_root, "resolved_root": session.active_root},
                        filters={},
                        actions=[{"tool": "select_pending_result"}],
                        needs_clarification=False,
                    )

        path = self._extract_explicit_path(text)
        if path is not None and not self._looks_like_root_request(text):
            return RequestPipelineResult(
                intent="read_file",
                requires_tool=True,
                response_allowed=False,
                target={"type": "file", "path": path},
                scope={"root_alias": None, "resolved_root": None},
                filters={},
                actions=[{"tool": "read_file"}],
                needs_clarification=False,
            )

        if self._looks_like_root_request(text):
            root = self._extract_root_path(text)
            if root is not None:
                return RequestPipelineResult(
                    intent="count_files",
                    requires_tool=True,
                    response_allowed=False,
                    target={"type": "root", "path": root},
                    scope={"root_alias": self._extract_root_alias(text), "resolved_root": root},
                    filters={"extension": self._extract_extension(text), "recursive": True},
                    actions=[{"tool": "count_files"}],
                    needs_clarification=False,
                )

        if path is not None:
            return RequestPipelineResult(
                intent="read_file",
                requires_tool=True,
                response_allowed=False,
                target={"type": "file", "path": path},
                scope={"root_alias": None, "resolved_root": None},
                filters={},
                actions=[{"tool": "read_file"}],
                needs_clarification=False,
            )

        filename_lookup = self._extract_indexed_filename_lookup(text)
        if filename_lookup is None:
            filename_lookup = self._extract_file_name_clause_lookup(text)
        if filename_lookup is None:
            filename_lookup = self._extract_filename_lookup(text)
        if filename_lookup is not None and not self._is_file_search(text, filename_lookup):
            filename_lookup = None
        if filename_lookup is not None:
            return RequestPipelineResult(
                intent="find_files",
                requires_tool=True,
                response_allowed=False,
                target={"type": "filename", "query": filename_lookup, "requested_action": self._extract_requested_action(text)},
                scope={"root_alias": None, "resolved_root": None},
                filters={"name": filename_lookup},
                actions=[{"tool": "find_files"}],
                needs_clarification=False,
            )

        if self._is_summarize_request(text):
            return RequestPipelineResult(intent="respond", requires_tool=False, response_allowed=True)

        return RequestPipelineResult(intent="respond", requires_tool=False, response_allowed=True)

    def _extract_explicit_path(self, text: str) -> str | None:
        for match in re.finditer(r"([A-Za-z]:[\\/][^\"'\n\r]+)", text):
            candidate = match.group(1).strip().rstrip(".,;:)")
            if candidate:
                return candidate.replace("\\", "/")

        for match in re.finditer(r"(?:^|\s)(/[^\s\"']+/[^\s\"']+)", text):
            candidate = match.group(1).strip().rstrip(".,;:)")
            if candidate:
                return candidate

        for match in re.finditer(r"(?:^|\s)([A-Za-z0-9_.-]+\\[A-Za-z0-9_.\\-]+)", text):
            candidate = match.group(1)
            if os.path.exists(candidate):
                return candidate

        return None

    def _is_summarize_request(self, text: str) -> bool:
        lowered = text.lower()
        return "summarize" in lowered or "summary" in lowered

    _FILE_WORDS = re.compile(
        r"\b(?:files?|folders?|directory|directories|documents?|indexed|pdfs?|spreadsheets?|paths?|filename)\b",
        re.IGNORECASE,
    )
    _FILENAME_LIKE = re.compile(r"[A-Za-z0-9_.-]+\.(?:md|txt|pdf|xlsx|xls|docx|csv|json|py|ps1|cs|al|jsonl|yml|yaml|xml|toml|log)\b", re.IGNORECASE)
    _NOISE_QUERIES = frozenset({"to", "a", "an", "the", "me", "it", "up", "on", "in", "of", "for", "at", "and", "or", "my", "your"})

    _IMPERATIVE_FIND = re.compile(r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+)?(?:find|locate|search(?:\s+for)?)\b", re.IGNORECASE)
    _QUESTION_WORDS = frozenset({"how", "what", "why", "when", "where", "which", "who", "whether"})

    def _is_file_search(self, text: str, query: str) -> bool:
        normalized = query.strip().lower()
        if len(normalized) < 2 or normalized in self._NOISE_QUERIES:
            return False
        if self._FILE_WORDS.search(text) or self._FILENAME_LIKE.search(text):
            return True
        if self._extract_explicit_path(text) is not None:
            return True
        words = normalized.split()
        if self._IMPERATIVE_FIND.match(text) and len(words) <= 4 and words[0] not in self._QUESTION_WORDS:
            return True
        return False

    def _has_file_command_verb(self, lowered: str) -> bool:
        return any(token in lowered for token in ("find", "locate", "lookup", "search", "list", "show", "display"))

    def _looks_like_selection(self, text: str) -> bool:
        lowered = text.strip().lower()
        return lowered.isdigit() or lowered in {"the first one", "the second one", "that file", "this file"}

    def _extract_selection(self, text: str) -> int | None:
        lowered = text.strip().lower()
        if lowered.isdigit():
            return int(lowered)
        if lowered == "the first one":
            return 1
        if lowered == "the second one":
            return 2
        return None

    def _looks_like_root_request(self, text: str) -> bool:
        lowered = text.lower()
        if any(token in lowered for token in ("summarize", "read", "open", "find", "locate", "lookup", "search", "list", "show", "display")):
            return False
        if not any(token in lowered for token in ("how many", "count", "number of", "total")):
            return False
        return any(token in lowered for token in ("file", "files", "folder", "directory", "root", "in ", "under", "within"))

    def _extract_root_path(self, text: str) -> str | None:
        explicit = self._extract_explicit_path(text)
        if explicit is not None:
            return explicit
        alias = self._extract_root_alias(text)
        if alias is None:
            return None
        return self._resolve_root(text)

    def _extract_extension(self, text: str) -> str:
        lowered = text.lower()
        matches = re.findall(r"\.(md|txt|pdf|xlsx|xls|json|py|ps1|cs|jsonl|yml|yaml|xml|toml)", lowered)
        if matches:
            return f".{matches[0]}"
        return ".md"

    def _extract_requested_action(self, text: str) -> str:
        lowered = text.lower()
        if "compare" in lowered:
            return "compare_files"
        if "open" in lowered:
            return "open_path"
        if "list" in lowered or "show" in lowered or "display" in lowered:
            return "list_matches"
        if "summarize" in lowered or "summary" in lowered:
            return "summarize_file"
        return "select_file"

    def _extract_filename_lookup(self, text: str) -> str | None:
        lowered = text.lower()
        if not self._has_file_command_verb(lowered):
            return None

        keyword_match = re.search(r"(?:find|locate|lookup|search\s+for|search|list|show|display)\s+(.+)", text, re.IGNORECASE)
        if keyword_match is not None:
            candidate = keyword_match.group(1).strip()
            candidate = re.split(r"\s+(?:and|then|to|for|with|that|this|or)\b", candidate, flags=re.IGNORECASE)[0]
            candidate = self._normalize_filename_query(candidate)
            if candidate:
                return candidate

        match = re.search(r"([A-Za-z0-9_.-]+\s+[A-Za-z0-9_.-]+\.(?:md|txt|pdf|xlsx|xls|json|py|ps1|cs|jsonl|yml|yaml|xml|toml))", text)
        if match is None:
            match = re.search(r"([A-Za-z0-9_.-]+\.(?:md|txt|pdf|xlsx|xls|json|py|ps1|cs|jsonl|yml|yaml|xml|toml))", text)
        if match is None:
            return None
        return match.group(1).strip()

    def _extract_file_name_clause_lookup(self, text: str) -> str | None:
        lowered = text.lower()
        if not any(token in lowered for token in ("file", "files")):
            return None
        if not self._has_file_command_verb(lowered):
            return None

        quoted = re.search(r"['\"]([^'\"]{2,120})['\"]", text)
        if quoted is not None:
            candidate = quoted.group(1).strip(" .,:;")
            if candidate:
                return candidate

        with_clause = re.search(r"\bwith\s+(.+?)\s+(?:in\s+the\s+name|name|filename)\b", text, re.IGNORECASE)
        if with_clause is not None:
            candidate = self._normalize_filename_query(with_clause.group(1))
            if candidate:
                return candidate

        for_clause = re.search(r"\bfor\s+(.+?)\s+(?:in\s+the\s+name|name|filename)\b", text, re.IGNORECASE)
        if for_clause is not None:
            candidate = self._normalize_filename_query(for_clause.group(1))
            if candidate:
                return candidate

        contain_clause = re.search(r"\bcontain(?:s|ing)?\s+(.+?)\s+(?:in\s+the\s+name|name|filename)\b", text, re.IGNORECASE)
        if contain_clause is not None:
            candidate = self._normalize_filename_query(contain_clause.group(1))
            if candidate:
                return candidate

        generic_clause = re.search(r"\b(?:with|for|contain(?:s|ing)?|matching|named|called)\s+(.+?)(?:\s+in\s+the\s+name|\s+name|\s+filename|$)", text, re.IGNORECASE)
        if generic_clause is not None:
            candidate = self._normalize_filename_query(generic_clause.group(1))
            if candidate:
                return candidate

        named_clause = re.search(r"\b(?:named|called)\s+(.+?)\b(?:$|\?|\.|,)", text, re.IGNORECASE)
        if named_clause is not None:
            candidate = self._normalize_filename_query(named_clause.group(1))
            if candidate:
                return candidate

        return None

    def _extract_indexed_filename_lookup(self, text: str) -> str | None:
        lowered = text.lower()
        if not any(token in lowered for token in ("indexed", "index")):
            return None
        if not any(token in lowered for token in ("file", "files")):
            return None

        quoted = re.search(r"['\"]([^'\"]{2,120})['\"]", text)
        if quoted is not None:
            candidate = quoted.group(1).strip(" .,:;")
            if candidate:
                return candidate

        with_clause = re.search(r"\bwith\s+(.+?)\s+(?:in\s+the\s+name|name|filename)\b", text, re.IGNORECASE)
        if with_clause is not None:
            candidate = self._normalize_filename_query(with_clause.group(1))
            if candidate:
                return candidate

        for_clause = re.search(r"\bfor\s+(.+?)\s+(?:in\s+the\s+name|name|filename)\b", text, re.IGNORECASE)
        if for_clause is not None:
            candidate = self._normalize_filename_query(for_clause.group(1))
            if candidate:
                return candidate

        contain_clause = re.search(r"\bcontain(?:s|ing)?\s+(.+?)\s+(?:in\s+the\s+name|name|filename)\b", text, re.IGNORECASE)
        if contain_clause is not None:
            candidate = self._normalize_filename_query(contain_clause.group(1))
            if candidate:
                return candidate

        generic_clause = re.search(r"\b(?:with|for|contain(?:s|ing)?|matching|named|called)\s+(.+?)(?:\s+in\s+the\s+name|\s+name|\s+filename|$)", text, re.IGNORECASE)
        if generic_clause is not None:
            candidate = self._normalize_filename_query(generic_clause.group(1))
            if candidate:
                return candidate

        named_clause = re.search(r"\b(?:named|called)\s+(.+?)\b(?:$|\?|\.|,)", text, re.IGNORECASE)
        if named_clause is not None:
            candidate = self._normalize_filename_query(named_clause.group(1))
            if candidate:
                return candidate

        return None

    def _normalize_filename_query(self, candidate: str) -> str:
        value = str(candidate or "").strip(" .,:;\t\n\r")
        if not value:
            return ""

        value = re.sub(r"\s+", " ", value).strip()
        value = re.sub(r"\b(?:in\s+the\s+name|name|filename)\b\s*$", "", value, flags=re.IGNORECASE).strip(" .,:;")
        changed = True
        while changed and value:
            previous = value
            value = re.sub(r"^me\b[\s,:-]*", "", value, flags=re.IGNORECASE).strip()
            value = re.sub(r"^(?:any|all|the|this|that|these|those|anything|something|file|files)\b[\s,:-]*", "", value, flags=re.IGNORECASE).strip()
            value = re.sub(r"^(?:first|top)\s+\d+\b[\s,:-]*", "", value, flags=re.IGNORECASE).strip()
            value = re.sub(r"^(?:indexed\s+)?files?\b[\s,:-]*", "", value, flags=re.IGNORECASE).strip()
            value = re.sub(r"^(?:that|which)\b[\s,:-]*", "", value, flags=re.IGNORECASE).strip()
            value = re.sub(r"^(?:have|has|with|for|like|named|called|matching|contain|contains|containing|including)\b[\s,:-]*", "", value, flags=re.IGNORECASE).strip()
            changed = value != previous

        value = re.sub(r"\s+", " ", value).strip(" .,:;")
        return value

    def _extract_root_alias(self, text: str) -> str | None:
        lowered = text.lower()
        if "ai" in lowered:
            return "AI"
        return None

    def _resolve_root(self, text: str) -> str | None:
        alias = self._extract_root_alias(text)
        if alias is None:
            return None

        configured = self._find_configured_root(alias)
        if configured is not None:
            return configured

        if alias == "AI":
            return "E:/AI"
        return None

    def _find_configured_root(self, alias: str) -> str | None:
        roots = self.config.get("roots") if isinstance(self.config, dict) else None
        if isinstance(roots, list):
            for item in roots:
                if isinstance(item, str):
                    if item.lower() == alias.lower():
                        return item
                elif isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    if name.lower() == alias.lower():
                        return str(item.get("path", "")).strip()
                    aliases = item.get("aliases")
                    if isinstance(aliases, list):
                        for candidate in aliases:
                            if isinstance(candidate, str) and candidate.lower() == alias.lower():
                                return str(item.get("path", "")).strip()

        document_search = self.config.get("document_search") if isinstance(self.config, dict) else None
        if isinstance(document_search, dict):
            roots = document_search.get("roots")
            if isinstance(roots, list):
                for item in roots:
                    if isinstance(item, DocumentSearchRoot) and str(item.path).lower() == alias.lower():
                        return str(item.path)
                    if isinstance(item, str) and item.lower() == alias.lower():
                        return item
                    if isinstance(item, dict) and str(item.get("path", "")).strip().lower() == alias.lower():
                        return str(item.get("path", "")).strip()

        return None
