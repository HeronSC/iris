# File: core/assistant/intent_example_store.py

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IntentExample:
    utterance: str
    intent: str
    arguments: dict[str, Any]
    source: str
    corrected_from: str | None = None


class IntentExampleStore:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.store_path.parent.mkdir(parents=True, exist_ok=True)

    def resolve(self, utterance: str) -> IntentExample | None:
        normalized = self._normalize(utterance)
        for item in self._load_examples():
            if self._normalize(str(item.get("utterance", ""))) != normalized:
                continue
            intent = str(item.get("intent", "")).strip()
            arguments = item.get("arguments", {})
            if not intent or not isinstance(arguments, dict):
                continue
            return IntentExample(
                utterance=str(item.get("utterance", "")),
                intent=intent,
                arguments=arguments,
                source=str(item.get("source", "example-store")),
                corrected_from=item.get("corrected_from") if isinstance(item.get("corrected_from"), str) else None,
            )
        return None

    def save_example(self, utterance: str, intent: str, arguments: dict[str, Any], source: str, corrected_from: str | None = None) -> None:
        payload = self._load_payload()
        examples = payload.setdefault("examples", [])
        normalized = self._normalize(utterance)
        record = {
            "utterance": utterance,
            "intent": intent,
            "arguments": arguments,
            "source": source,
            "corrected_from": corrected_from,
            "updated_at": self._utc_now_iso(),
        }
        replaced = False
        for index, item in enumerate(examples):
            if self._normalize(str(item.get("utterance", ""))) == normalized:
                examples[index] = record
                replaced = True
                break
        if not replaced:
            examples.append(record)
        self._write_payload(payload)

    def render_examples_for_prompt(self, limit: int = 20) -> str:
        examples = self._load_examples()
        if not examples:
            return ""
        lines: list[str] = []
        for item in examples[-limit:]:
            utterance = str(item.get("utterance", "")).strip()
            intent = str(item.get("intent", "")).strip()
            arguments = item.get("arguments", {})
            if not utterance or not intent or not isinstance(arguments, dict):
                continue
            lines.append(f"- '{utterance}' -> {{\"intent\":\"{intent}\",\"arguments\":{json.dumps(arguments, ensure_ascii=False)}}}")
        return "\n".join(lines)

    def _load_examples(self) -> list[dict[str, Any]]:
        payload = self._load_payload()
        examples = payload.get("examples", [])
        if not isinstance(examples, list):
            return []
        return [item for item in examples if isinstance(item, dict)]

    def _load_payload(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return {"schema_version": 1, "examples": []}
        try:
            with self.store_path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, RuntimeError, TypeError):
            return {"schema_version": 1, "examples": []}
        if not isinstance(payload, dict):
            return {"schema_version": 1, "examples": []}
        return payload

    def _write_payload(self, payload: dict[str, Any]) -> None:
        with self.store_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    def _normalize(self, utterance: str) -> str:
        return " ".join(utterance.strip().lower().split())

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()