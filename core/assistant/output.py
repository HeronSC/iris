# File: core/assistant/output.py

from __future__ import annotations

import sys
from collections.abc import Callable


OutputSink = Callable[[str, str | None], None]


def emit_output(sink: OutputSink | None, text: str, role: str | None = None) -> None:
    if sink is None:
        sys.stdout.write(f"{text}\n")
        return
    sink(text, role)
