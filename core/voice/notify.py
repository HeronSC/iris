# File: core/voice/notify.py

from __future__ import annotations

from typing import Any


class SpokenNotifier:
    name = "voice"

    def __init__(self, voice: Any) -> None:
        self.voice = voice

    def send(self, notification: Any) -> None:
        title = str(getattr(notification, "title", "") or "").strip().rstrip(".")
        body = str(getattr(notification, "body", "") or "").strip()
        self.voice.speak(f"{title}. {body}" if title else body)
