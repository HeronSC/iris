from __future__ import annotations

from collections.abc import Callable


OutputSink = Callable[[str, str | None], None]


def emit_output(sink: OutputSink | None, text: str, role: str | None = None) -> None:
    if sink is None:
        print(text)
        return
    sink(text, role)
