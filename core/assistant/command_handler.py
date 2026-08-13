from __future__ import annotations

from typing import Protocol


class CommandHandler(Protocol):
    def handle(self, user_input: str, state: dict[str, object]) -> bool:
        ...
