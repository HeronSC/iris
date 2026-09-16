# File: core/voice/stream.py

from __future__ import annotations

import re
from typing import Callable

from core.voice.text import speech_text

SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n{2,}")
MIN_SENTENCE_CHARS = 12


class SentenceStreamer:
    def __init__(self, speak: Callable[[str], bool], *, limit: int = 600) -> None:
        self.speak = speak
        self.limit = limit
        self.buffer = ""
        self.spoken_chars = 0
        self.sentences: list[str] = []

    @property
    def started(self) -> bool:
        return bool(self.sentences)

    def reset(self) -> None:
        self.buffer = ""
        self.spoken_chars = 0
        self.sentences = []

    def feed(self, delta: str) -> None:
        self.buffer += delta or ""
        if self.buffer.count("```") % 2 == 1:
            return
        while True:
            match = next((m for m in SENTENCE_BREAK.finditer(self.buffer) if len(self.buffer[: m.start()].strip()) >= MIN_SENTENCE_CHARS), None)
            if match is None:
                return
            head = self.buffer[: match.start()]
            self.buffer = self.buffer[match.end() :]
            self._emit(head)

    def finish(self) -> None:
        rest, self.buffer = self.buffer, ""
        self._emit(rest)

    def _emit(self, raw: str) -> None:
        if self.spoken_chars >= self.limit:
            return
        text = speech_text(raw, max(0, self.limit - self.spoken_chars))
        if not text:
            return
        self.spoken_chars += len(text)
        self.sentences.append(text)
        self.speak(text)
