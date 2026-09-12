# File: core/actions/implementations/memory_tools.py

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.assistant.learning import LearningLoop
from core.knowledge.facts import FactService, describe_record, find_conflicts
from core.knowledge.models import MemoryKind, MemoryRecord
from core.knowledge.scopes import GLOBAL
from core.results.html import compose_url, run_url
from core.results.models import Source, status, table
from core.tools.models import PermissionLevel, ToolDefinition

NO_SERVICE = "Memory is not available in this host."
KIND_NAMES = {kind.value: kind for kind in MemoryKind}


class BrowseArguments(BaseModel):
    topic: str | None = Field(default=None, description="Only records in this topic, such as trading/candidates; every topic when omitted")
    kind: str | None = Field(default=None, description="Only this kind: fact, observation, outcome, decision, hypothesis, knowledge")
    limit: int = Field(default=20, ge=1, le=200)


class MemoryBrowseAction:

    name = "memory_browse"
    definition = ToolDefinition(
        name="memory_browse",
        description="List what Iris remembers, newest first, with id, kind, status, scope, date, topic and text, so a record can be shown, explained, corrected or retired. Use when the user asks to see, review, clean up or fix Iris's memory.",
        arguments=BrowseArguments,
        permission=PermissionLevel.READ,
        keywords=("what do you remember", "show your memory", "browse memory", "list memories", "what have you recorded", "review memory", "clean up memory", "memories about"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = BrowseArguments.model_validate(request.arguments)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if getattr(context, "knowledge", None) is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        kind = (arguments.kind or "").strip().lower() or None
        if kind and kind not in KIND_NAMES:
            return ValidationResult(ok=False, error=f"kind is one of {', '.join(KIND_NAMES)}")
        return ValidationResult(ok=True, resolved_target=arguments.topic, resolved_arguments={"topic": (arguments.topic or "").strip().lower() or None, "kind": kind, "limit": arguments.limit})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        knowledge = getattr(context, "knowledge", None)
        if knowledge is None:
            return ActionResult(status="failed", message=NO_SERVICE, action=self.name, error="no_service")
        arguments = request.arguments
        kind = KIND_NAMES.get(str(arguments.get("kind") or ""))
        records = FactService(knowledge.records).browse(arguments.get("topic"), limit=int(arguments.get("limit") or 20), kinds=(kind,) if kind else ())
        source = Source("memory_browse", "memory", arguments.get("topic") or "all topics")
        if not records:
            message = "No records" + (f" in {arguments['topic']}" if arguments.get("topic") else "") + "."
            return ActionResult(status="success", message=message, action=self.name, results=(status("ok", message, source=source),))
        rows: list[tuple[Any, ...]] = []
        actions: list[list[dict[str, str]]] = []
        for record in records:
            short = record.id[:8]
            rows.append((short, record.kind.value, record.status.value, "" if record.scope == GLOBAL else record.scope, record.created_at[:10], record.topic, record.content[:160]))
            actions.append(
                [
                    {"label": "show", "href": run_url(f"/knowledge show {short}")},
                    {"label": "why", "href": run_url(f"/knowledge why {short}")},
                    {"label": "supersede", "href": compose_url(f"/knowledge supersede {short} ")},
                    {"label": "forget", "href": compose_url(f"/knowledge forget {short} ")},
                ]
            )
        heading = f"{len(records)} record{'s' if len(records) != 1 else ''}" + (f" in {arguments['topic']}" if arguments.get("topic") else ", newest first")
        lines = [heading] + [describe_record(record) for record in records]
        result = table(("id", "kind", "status", "scope", "date", "topic", "content"), rows, source=source, title="Iris memory")
        result.data["row_actions"] = actions
        return ActionResult(status="success", message="\n".join(lines), action=self.name, resolved_target=arguments.get("topic"), results=(result,))


class GapArguments(BaseModel):
    question: str = Field(description="The question Iris could not answer, as a full sentence")
    context: str = Field(default="", description="What was tried or why it is unknown")


class RecordGapAction:

    name = "record_gap"
    definition = ToolDefinition(
        name="record_gap",
        description="Record something Iris does not know as an open question, instead of guessing. Use it after saying plainly that the answer is not known and no tool found it. The user closes it later with /knowledge outcome.",
        arguments=GapArguments,
        permission=PermissionLevel.WRITE,
        keywords=("i do not know", "unknown", "open question", "cannot find out", "record the gap"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = GapArguments.model_validate(request.arguments)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if getattr(context, "knowledge", None) is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        if not arguments.question.strip():
            return ValidationResult(ok=False, error="The question is empty")
        return ValidationResult(ok=True, resolved_target=arguments.question[:80], resolved_arguments={"question": " ".join(arguments.question.split()), "context": arguments.context.strip()})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        knowledge = getattr(context, "knowledge", None)
        if knowledge is None:
            return ActionResult(status="failed", message=NO_SERVICE, action=self.name, error="no_service")
        record = LearningLoop(knowledge).record_gap(str(request.arguments.get("question") or ""), context=str(request.arguments.get("context") or ""))
        if record is None:
            return ActionResult(status="failed", message="The question was empty.", action=self.name, error="empty")
        message = f"Recorded as an open question ({record.id[:8]}): {record.content}. /knowledge gaps lists them; /knowledge outcome {record.id[:8]} <answer> closes it."
        return ActionResult(status="success", message=message, action=self.name, resolved_target=record.id, results=(status("pending", message, source=Source("record_gap", "memory", record.id)),))


class NoteArguments(BaseModel):
    topic: str = Field(description="Where it belongs, such as research/bc-licensing or home/network")
    text: str = Field(description="The finding, in one or two sentences")
    url: str | None = Field(default=None, description="The page it came from, when it came from the web")


class MemoryNoteAction:

    name = "memory_note"
    definition = ToolDefinition(
        name="memory_note",
        description="Keep a finding in Iris's memory as a fact under a topic, with its source URL when it came from the web. Use when the user says to remember or keep something, or when a research answer is worth keeping.",
        arguments=NoteArguments,
        permission=PermissionLevel.WRITE,
        keywords=("remember this", "keep that", "note that", "save this finding", "make a note", "remember that"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = NoteArguments.model_validate(request.arguments)
        except ValidationError as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        if getattr(context, "knowledge", None) is None:
            return ValidationResult(ok=False, error=NO_SERVICE)
        if not arguments.text.strip() or not arguments.topic.strip():
            return ValidationResult(ok=False, error="A note needs a topic and text")
        return ValidationResult(ok=True, resolved_target=arguments.topic.strip().lower(), resolved_arguments={"topic": arguments.topic.strip().lower(), "text": " ".join(arguments.text.split()), "url": (arguments.url or "").strip() or None})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        knowledge = getattr(context, "knowledge", None)
        if knowledge is None:
            return ActionResult(status="failed", message=NO_SERVICE, action=self.name, error="no_service")
        url = request.arguments.get("url")
        topic = str(request.arguments["topic"])
        content = str(request.arguments["text"])
        conflicts = find_conflicts(knowledge.records, topic, content, scope=GLOBAL)
        same = next((item for item in conflicts if item.similarity >= 1.0), None)
        if same is not None:
            message = f"Already kept as {same.record.id[:8]}: {same.record.content[:80]}"
            return ActionResult(status="failed", message=message, action=self.name, error="duplicate")
        record = knowledge.records.add(MemoryRecord(kind=MemoryKind.FACT, topic=topic, content=content, source="web" if url else "iris:note", source_ref=url, scope=GLOBAL))
        message = f"Kept as {record.id[:8]} under {record.topic}" + (f" (source {url})" if url else "") + "."
        if conflicts:
            message += f" It may conflict with {len(conflicts)} earlier fact(s); /knowledge browse {record.topic} shows them."
        return ActionResult(status="success", message=message, action=self.name, resolved_target=record.id, results=(status("ok", message, source=Source("memory_note", "memory", url or record.id)),))


MEMORY_ACTIONS = (MemoryBrowseAction, RecordGapAction, MemoryNoteAction)

__all__ = ["MEMORY_ACTIONS", "MemoryBrowseAction", "MemoryNoteAction", "RecordGapAction"]
