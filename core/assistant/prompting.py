from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PromptType(str, Enum):
    YES_NO_CANCEL = "yes_no_cancel"
    TEXT = "text"


PROMPT_CANCEL_TOKEN = "__iris_prompt_cancel__"


@dataclass(frozen=True)
class PromptRequest:
    prompt_id: str
    prompt_type: PromptType
    text: str
    sensitive: bool = False
    choices: tuple[str, ...] = ()
