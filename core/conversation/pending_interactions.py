from __future__ import annotations

from typing import Any


class PendingInteractionStore:
    def __init__(self, session: Any) -> None:
        self.session = session

    def set_pending(self, payload: dict[str, Any]) -> None:
        self.session.pending_interaction = payload

    def clear(self) -> None:
        self.session.pending_interaction = None

    def get(self) -> dict[str, Any] | None:
        return self.session.pending_interaction
