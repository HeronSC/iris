from typing import Any

from core.application.contracts import (
    ActionSuggestion,
    ConfirmationContent,
    ConversationContent,
    DetailContent,
    ErrorContent,
    TopicContext,
    IrisEvent,
    IrisMessage,
    IrisResponse,
    IrisStatus,
    MessageRole,
)

__all__ = [
    "IrisApplication",
    "IrisResponse",
    "IrisEvent",
    "IrisMessage",
    "IrisStatus",
    "MessageRole",
    "ActionSuggestion",
    "ConversationContent",
    "DetailContent",
    "TopicContext",
    "ConfirmationContent",
    "ErrorContent",
]


def __getattr__(name: str) -> Any:
    if name == "IrisApplication":
        from core.application.service import IrisApplication

        return IrisApplication
    raise AttributeError(name)
