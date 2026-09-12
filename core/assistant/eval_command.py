# File: core/assistant/eval_command.py

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.assistant.learning import CORRECTIONS_TOPIC
from core.assistant.output import OutputSink, emit_output
from core.assistant.request_kinds import is_code_request, is_small_talk
from core.knowledge.principles import GAP_TOPIC
from core.knowledge.retrieval import KnowledgeQuery

USAGE = "Usage: /eval | /eval add <kind|intent=<name>|memory=<text>> :: <request> | /eval list | /eval failures"
KINDS = ("small_talk", "code", "find_files", "count_files", "read_file", "respond", "none", "select_pending_result")
Classifier = Callable[[str], dict[str, Any]]


def classify(text: str, pipeline: Any = None) -> dict[str, Any]:
    intent = "respond"
    requires_tool = False
    if pipeline is not None:
        result = pipeline.build_request(text)
        intent = str(getattr(result, "intent", "respond") or "respond")
        requires_tool = bool(getattr(result, "requires_tool", False))
    if is_small_talk(text):
        kind = "small_talk"
    elif is_code_request(text):
        kind = "code"
    else:
        kind = intent
    return {"kind": kind, "intent": intent, "requires_tool": requires_tool}


def parse_expectation(spec: str) -> dict[str, str]:
    spec = spec.strip()
    if not spec:
        raise ValueError("Say what to expect: a kind, intent=<name> or memory=<text>")
    if "=" in spec:
        key, _, value = spec.partition("=")
        key = key.strip().lower()
        if key not in {"kind", "intent", "memory"}:
            raise ValueError("Expectations are kind, intent or memory")
        if not value.strip():
            raise ValueError(f"{key} needs a value")
        return {key: value.strip()}
    if spec.lower() not in KINDS:
        raise ValueError(f"Unknown kind '{spec}'; one of {', '.join(KINDS)}")
    return {"kind": spec.lower()}


class EvalCommandHandler:
    def __init__(self, folder: str | Path, *, output: OutputSink | None = None, pipeline: Any = None, retriever: Any = None, knowledge: Any = None, classifier: Classifier | None = None) -> None:
        self.folder = Path(folder)
        self.output = output
        self.pipeline = pipeline
        self.retriever = retriever
        self.knowledge = knowledge
        self.classifier = classifier or (lambda text: classify(text, self.pipeline))

    @property
    def requests_path(self) -> Path:
        return self.folder / "requests.jsonl"

    @property
    def runs_path(self) -> Path:
        return self.folder / "runs.jsonl"

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        text = user_input.strip()
        if not text.lower().startswith("/eval"):
            return False
        rest = text[5:].strip()
        if not rest:
            return self._run()
        command, _, remainder = rest.partition(" ")
        command = command.lower()
        if command == "add":
            return self._add(remainder)
        if command == "list":
            return self._list()
        if command == "failures":
            return self._failures()
        emit_output(self.output, USAGE)
        return True

    def cases(self) -> list[dict[str, Any]]:
        if not self.requests_path.is_file():
            return []
        found = []
        for line in self.requests_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("text") and isinstance(item.get("expect"), dict):
                found.append(item)
        return found

    def add_case(self, text: str, expect: dict[str, str]) -> dict[str, Any]:
        self.folder.mkdir(parents=True, exist_ok=True)
        item = {"text": " ".join(text.split()), "expect": expect, "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with self.requests_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item

    def run(self) -> dict[str, Any]:
        cases = self.cases()
        results = []
        for item in cases:
            expect = item["expect"]
            observed = self.classifier(item["text"])
            memory_hits: list[str] = []
            if "memory" in expect:
                memory_hits = self._memory_hits(item["text"])
                observed["memory"] = memory_hits
            failures = []
            if "kind" in expect and observed.get("kind") != expect["kind"]:
                failures.append(f"kind {observed.get('kind')} (expected {expect['kind']})")
            if "intent" in expect and observed.get("intent") != expect["intent"]:
                failures.append(f"intent {observed.get('intent')} (expected {expect['intent']})")
            if "memory" in expect and not any(expect["memory"].casefold() in hit.casefold() for hit in memory_hits):
                failures.append(f"memory did not surface '{expect['memory']}'")
            results.append({"text": item["text"], "ok": not failures, "failures": failures})
        passed = sum(1 for item in results if item["ok"])
        previous = self._last_run()
        regressions = []
        if previous:
            passed_before = set(previous.get("passed_texts") or [])
            regressions = [item["text"] for item in results if not item["ok"] and item["text"] in passed_before]
        summary = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "total": len(results), "passed": passed, "passed_texts": [item["text"] for item in results if item["ok"]], "regressions": regressions}
        if results:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self.runs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
        return {"results": results, "summary": summary, "previous": previous}

    def _memory_hits(self, text: str) -> list[str]:
        if self.retriever is None:
            return []
        try:
            found = self.retriever.retrieve(KnowledgeQuery(text=text))
        except (OSError, ValueError, RuntimeError, TypeError):
            return []
        return [record.content for record in found.as_records()[:5]]

    def _last_run(self) -> dict[str, Any] | None:
        if not self.runs_path.is_file():
            return None
        lines = [line for line in self.runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return None
        try:
            item = json.loads(lines[-1])
        except json.JSONDecodeError:
            return None
        return item if isinstance(item, dict) else None

    def _run(self) -> bool:
        report = self.run()
        results = report["results"]
        if not results:
            emit_output(self.output, f"No saved requests yet. Add one with /eval add <kind> :: <request>; they live in {self.requests_path}.")
            return True
        summary = report["summary"]
        emit_output(self.output, f"{summary['passed']} of {summary['total']} passed.")
        for item in results:
            if not item["ok"]:
                emit_output(self.output, f"- FAIL {item['text'][:80]}: {'; '.join(item['failures'])}")
        if summary["regressions"]:
            emit_output(self.output, f"{len(summary['regressions'])} regression(s) since the last run: " + "; ".join(text[:60] for text in summary["regressions"]))
        elif report["previous"]:
            emit_output(self.output, f"No regressions since the last run ({report['previous'].get('passed')} of {report['previous'].get('total')} then).")
        return True

    def _add(self, remainder: str) -> bool:
        spec, separator, text = remainder.partition("::")
        if not separator or not text.strip():
            emit_output(self.output, USAGE)
            return True
        try:
            expect = parse_expectation(spec)
        except ValueError as error:
            emit_output(self.output, str(error))
            return True
        item = self.add_case(text, expect)
        emit_output(self.output, f"Saved: '{item['text'][:60]}' expecting {', '.join(f'{k}={v}' for k, v in expect.items())}. {len(self.cases())} request(s) in the set.")
        return True

    def _list(self) -> bool:
        cases = self.cases()
        if not cases:
            emit_output(self.output, "No saved requests yet.")
            return True
        emit_output(self.output, f"{len(cases)} saved request(s):")
        for item in cases:
            emit_output(self.output, f"- {item['text'][:70]}  ->  " + ", ".join(f"{k}={v}" for k, v in item["expect"].items()))
        return True

    def _failures(self) -> bool:
        if self.knowledge is None:
            emit_output(self.output, "Memory is not available in this host.")
            return True
        corrections = self.knowledge.open_observations(CORRECTIONS_TOPIC)
        gaps = self.knowledge.open_observations(GAP_TOPIC)
        emit_output(self.output, f"{len(corrections)} correction(s) and {len(gaps)} open question(s) recorded as failures to learn from.")
        for item in list(corrections)[:10]:
            emit_output(self.output, f"- correction {item.created_at[:10]}: {item.content[:90]}")
        for item in list(gaps)[:10]:
            emit_output(self.output, f"- gap {item.created_at[:10]}: {item.content[:90]}")
        emit_output(self.output, "Turn one into a regression check with /eval add <kind> :: <request>.")
        return True


__all__ = ["EvalCommandHandler", "KINDS", "USAGE", "classify", "parse_expectation"]
