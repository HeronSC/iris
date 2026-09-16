# File: core/actions/implementations/recall_tools.py

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.results.models import Result, Source, status, text
from core.tools.models import PermissionLevel, ToolDefinition

_MAX_CHARS = 6000
_SNIPPET = 240


class RecallArguments(BaseModel):
    query: str = Field(default="", description="Words that should all appear in the message; leave empty for the most recent messages")
    days: int = Field(default=30, ge=1, le=3650, description="How far back to look")
    role: str = Field(default="any", description="'assistant' for things Iris said or wrote, 'user' for things the user said, or 'any'")
    limit: int = Field(default=5, ge=1, le=20)
    full_text: bool = Field(default=True, description="Return the full text of each match instead of a snippet")


def _words(query: str) -> list[str]:
    return [word for word in re.findall(r"[\w'-]+", (query or "").casefold()) if len(word) > 1]


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class RecallConversationAction:
    name = "recall_conversation"
    definition = ToolDefinition(
        name="recall_conversation",
        description=(
            "Search everything said in past conversations with Iris, across all sessions, for messages containing the given words, "
            "newest first, and return their full text. Also finds documents Iris saved for the user. Use whenever the user refers to "
            "something from an earlier conversation: a story, letter, or document Iris wrote, an answer it gave, or a decision made."
        ),
        arguments=RecallArguments,
        permission=PermissionLevel.READ,
        timeout_seconds=30.0,
        cost="reads saved conversation files",
        keywords=(
            "yesterday",
            "last time",
            "earlier conversation",
            "previous conversation",
            "do you still have",
            "that story",
            "we talked about",
            "we discussed",
            "you wrote",
            "you gave me",
            "remember when",
            "the other day",
        ),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            parsed = RecallArguments.model_validate(request.arguments)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        role = parsed.role.strip().lower() or "any"
        if role not in {"any", "user", "assistant"}:
            return ValidationResult(ok=False, error="role must be 'any', 'user' or 'assistant'")
        resolved = parsed.model_dump()
        resolved["role"] = role
        return ValidationResult(ok=True, resolved_target=parsed.query[:80], resolved_arguments=resolved)

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        repository = getattr(context, "session_repository", None)
        creations = getattr(context, "creations", None)
        if repository is None and creations is None:
            return ActionResult(status="failed", message="Past conversations are not available.", action=self.name, error="no_session_repository")
        arguments = request.arguments
        words = _words(str(arguments.get("query", "")))
        days = int(arguments.get("days") or 30)
        role = str(arguments.get("role") or "any")
        limit = int(arguments.get("limit") or 5)
        full_text = bool(arguments.get("full_text", True))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        source = Source("recall_conversation", "tool", " ".join(words) or "recent")

        results: list[Result] = []
        lines: list[str] = []
        saved = creations.search(words, limit=limit) if creations is not None else []
        for hit in saved:
            body = hit["text"] if full_text else hit["text"][:_SNIPPET]
            lines.append(f"Saved document: {hit['path']} (modified {hit['modified']})")
            lines.append(body[:_MAX_CHARS].rstrip())
            lines.append("")
            results.append(text(body[:_MAX_CHARS], source=source, title=hit["title"], ref=hit["path"]))

        hits = self._search_sessions(repository, words, cutoff=cutoff, role=role, limit=limit) if repository is not None else []
        for hit in hits:
            body = hit["content"] if full_text else hit["content"][:_SNIPPET]
            lines.append(f"[{hit['created_at']} | session {hit['session_id']} | {hit['role']}] {hit['title']}")
            lines.append(body[:_MAX_CHARS].rstrip())
            lines.append("")
            results.append(text(body[:_MAX_CHARS], source=source, title=f"{hit['title']} ({hit['created_at']}, {hit['role']})", ref=hit["session_id"]))

        if not lines:
            wanted = " ".join(words) or "anything"
            message = f"Nothing in the last {days} days of conversations mentions {wanted}."
            return ActionResult(status="success", message=message, action=self.name, results=(status("ok", message, source=source),))
        total = len(saved) + len(hits)
        header = f"{total} match{'es' if total != 1 else ''} in past conversations, newest first:"
        return ActionResult(status="success", message="\n".join([header, ""] + lines).rstrip(), action=self.name, results=tuple(results))

    def _search_sessions(self, repository: Any, words: list[str], *, cutoff: datetime, role: str, limit: int) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for entry in repository.list_sessions(limit=500):
            session_id = str(entry.get("id") or "")
            if not session_id:
                continue
            updated = _parse_time(entry.get("updated_at"))
            if updated is not None and updated < cutoff:
                continue
            try:
                session = repository.get_session(session_id)
            except (OSError, ValueError, RuntimeError, TypeError):
                continue
            if session is None:
                continue
            for message in session.get_messages():
                message_role = str(message.get("role") or "")
                if role != "any" and message_role != role:
                    continue
                if message_role not in {"user", "assistant"}:
                    continue
                content = str(message.get("content") or "")
                haystack = content.casefold()
                if words and not all(word in haystack for word in words):
                    continue
                created = _parse_time(message.get("created_at")) or updated
                if created is not None and created < cutoff:
                    continue
                hits.append(
                    {
                        "session_id": session_id,
                        "title": session.title,
                        "role": message_role,
                        "created_at": created.astimezone().strftime("%Y-%m-%d %H:%M") if created is not None else "",
                        "sort_key": created.isoformat() if created is not None else "",
                        "content": content,
                    }
                )
        hits.sort(key=lambda item: item["sort_key"], reverse=True)
        return hits[:limit]


RECALL_ACTIONS = (RecallConversationAction,)

__all__ = ["RECALL_ACTIONS", "RecallConversationAction"]
