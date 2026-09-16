# File: core/voice/__init__.py

from __future__ import annotations

from core.voice.config import DEFAULT_TALK_HOTKEY, VoiceConfig
from core.voice.errors import VoiceError
from core.voice.notify import SpokenNotifier
from core.voice.recorder import Recorder
from core.voice.service import VoiceService
from core.voice.speaker import Speaker
from core.voice.text import speech_text
from core.voice.transcriber import Transcriber

__all__ = ["DEFAULT_TALK_HOTKEY", "Recorder", "Speaker", "SpokenNotifier", "Transcriber", "VoiceConfig", "VoiceError", "VoiceService", "speech_text"]
