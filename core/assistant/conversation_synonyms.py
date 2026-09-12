# File: core/assistant/conversation_synonyms.py

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConversationAlias:
    phrase: str
    intent: str
    arguments: dict[str, Any]


class ConversationSynonymStore:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.store_path.parent.mkdir(parents=True, exist_ok=True)

    def resolve_pending_response(self, utterance: str) -> str | None:
        normalized = self._normalize(utterance)
        payload = self._load_payload()
        for candidate in payload.get("confirm", []):
            if self._normalize(str(candidate)) == normalized:
                return "confirm"
        for candidate in payload.get("cancel", []):
            if self._normalize(str(candidate)) == normalized:
                return "cancel"
        return None

    def resolve_alias(self, utterance: str) -> ConversationAlias | None:
        normalized = self._normalize(utterance)
        payload = self._load_payload()
        aliases = payload.get("aliases", [])
        if not isinstance(aliases, list):
            return None
        for item in aliases:
            if not isinstance(item, dict):
                continue
            phrase = str(item.get("phrase", "")).strip()
            intent = str(item.get("intent", "")).strip()
            arguments = item.get("arguments", {})
            if not phrase or not intent or not isinstance(arguments, dict):
                continue
            if self._normalize(phrase) != normalized:
                continue
            return ConversationAlias(phrase=phrase, intent=intent, arguments=arguments)
        return None

    def add_pending_response_phrases(self, category: str, phrases: list[str]) -> list[str]:
        normalized_category = category.strip().lower()
        if normalized_category not in {"confirm", "cancel"}:
            raise ValueError("category must be confirm or cancel")

        payload = self._load_payload()
        existing = payload.get(normalized_category, [])
        if not isinstance(existing, list):
            existing = []

        existing_by_normalized = {self._normalize(str(item)): str(item).strip() for item in existing if str(item).strip()}
        added: list[str] = []
        for phrase in phrases:
            cleaned = phrase.strip()
            normalized = self._normalize(cleaned)
            if not cleaned or not normalized or normalized in existing_by_normalized:
                continue
            existing.append(cleaned)
            existing_by_normalized[normalized] = cleaned
            added.append(cleaned)

        payload[normalized_category] = existing
        self._write_payload(payload)
        return added

    def log_phrase(self, log_path: Path, phrase: str, mapped_to: str, pending_action: str | None = None) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "phrase": phrase,
            "mapped_to": mapped_to,
            "pending_action": pending_action,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load_payload(self) -> dict[str, Any]:
        if not self.store_path.exists():
            payload = self._default_payload()
            self._write_payload(payload)
            return payload
        try:
            with self.store_path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, RuntimeError, TypeError):
            payload = self._default_payload()
            self._write_payload(payload)
            return payload
        if not isinstance(payload, dict):
            payload = self._default_payload()
            self._write_payload(payload)
            return payload
        changed = False
        defaults = self._default_payload()
        for key, value in defaults.items():
            if key not in payload:
                payload[key] = value
                changed = True
        if changed:
            self._write_payload(payload)
        return payload

    def _write_payload(self, payload: dict[str, Any]) -> None:
        with self.store_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    def _default_payload(self) -> dict[str, Any]:
        return {
            "confirm": [
                "yes",
                "yep",
                "yeah",
                "y",
                "ok",
                "okay",
                "sure",
                "absolutely",
                "approved",
                "approve",
                "go ahead",
                "do it",
                "sounds good",
                "proceed",
                "continue",
                "make it so",
                "let's do it",
                "confirm",
            ],
            "cancel": [
                "no",
                "nope",
                "cancel",
                "never mind",
                "forget it",
                "stop",
                "don't",
                "dont",
                "not now",
                "n",
            ],
            "aliases": [],
        }

    def _normalize(self, utterance: str) -> str:
        collapsed = " ".join(utterance.strip().lower().split())
        return re.sub(r"[.!?]+$", "", collapsed)