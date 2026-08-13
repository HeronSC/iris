from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    PROGRESS = "progress"
    ERROR = "error"
    CONFIRMATION = "confirmation"


class IrisStatus(str, Enum):
    READY = "ready"
    THINKING = "thinking"
    SEARCHING = "searching"
    INDEXING = "indexing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class ActionSuggestion:
    id: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationContent:
    message: str = ""
    suggested_actions: list[ActionSuggestion] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DetailContent:
    type: str = "text"
    section_id: str | None = None
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    items: list[Any] = field(default_factory=list)
    actions: list[ActionSuggestion] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TopicContext:
    id: str
    title: str
    relationship: str = "new_topic"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfirmationContent:
    prompt_id: str
    prompt_type: str
    text: str
    choices: tuple[str, ...] = ()
    sensitive: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ErrorContent:
    code: str | None = None
    message: str = ""
    details: str | None = None
    exception_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IrisMessage:
    role: MessageRole
    text: str


@dataclass(frozen=True)
class IrisEvent:
    status: IrisStatus
    message: IrisMessage | None = None
    progress_current: int | None = None
    progress_total: int | None = None


@dataclass(frozen=True)
class IrisResponse:
    messages: list[IrisMessage]
    status: IrisStatus
    response_type: str = "text"
    requires_confirmation: bool = False
    conversation: ConversationContent = field(default_factory=ConversationContent)
    topic: TopicContext | dict[str, Any] | None = None
    details: DetailContent | dict[str, Any] | None = None
    confirmation: ConfirmationContent | dict[str, Any] | None = None
    error: ErrorContent | dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
