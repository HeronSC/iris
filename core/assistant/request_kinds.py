# File: core/assistant/request_kinds.py

from __future__ import annotations

import re

_SMALL_TALK = re.compile(
    r"^\s*(?:hi|hello|hey|yo|good\s+(?:morning|afternoon|evening|night)|morning|evening|thanks?|thank\s+you|"
    r"cheers|bye|goodbye|see\s+you|how\s+are\s+you(?:\s+today|\s+doing)?|how'?s\s+it\s+going|what'?s\s+up|"
    r"nice|great|cool|ok(?:ay)?|sure|yes|no|lol|haha)"
    r"[\s,!.?]*(?:iris|there|again|everyone|all)?[\s,!.?]*$",
    re.IGNORECASE,
)

_CODE_REQUEST = re.compile(
    r"\b(?:function|procedure|codeunit|(?:write|generate|create|fix|show|give|review)\s+(?:me\s+)?(?:some\s+|the\s+|this\s+)?code|"
    r"code\s+(?:that|which|to|for)|script|snippet|class|method|regex|sql|query|algorithm|"
    r"implement|refactor|compile|syntax|stack\s*trace|exception|bug|debug|unit\s+test|"
    r"python|powershell|javascript|typescript|c#|csharp|java|rust|"
    r"al\s+code|business\s+central|api\s+call|endpoint|json\s+schema)\b",
    re.IGNORECASE,
)


def is_small_talk(text: str) -> bool:
    return bool(_SMALL_TALK.match(text or ""))


def is_code_request(text: str) -> bool:
    return bool(_CODE_REQUEST.search(text or ""))
