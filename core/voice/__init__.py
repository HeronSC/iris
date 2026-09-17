# File: core/voice/__init__.py

from __future__ import annotations

from core.voice.config import DEFAULT_TALK_HOTKEY, VoiceConfig
from core.voice.errors import VoiceError
from core.voice.notify import SpokenNotifier
from core.voice.recorder import Recorder
from core.voice.service import VoiceService
from core.voice.session import DEFAULT_END_PHRASES, DEFAULT_WAKE_NAMES, ConversationSession, is_end_phrase, match_wake
from core.voice.speaker import Speaker
from core.voice.stream import SentenceStreamer
from core.voice.text import speech_text
from core.voice.transcriber import Transcriber
from core.voice.vad import StreamingVad

__all__ = [
    "DEFAULT_END_PHRASES",
    "DEFAULT_TALK_HOTKEY",
    "DEFAULT_WAKE_NAMES",
    "ConversationSession",
    "Recorder",
    "SentenceStreamer",
    "Speaker",
    "SpokenNotifier",
    "StreamingVad",
    "Transcriber",
    "VoiceConfig",
    "VoiceError",
    "VoiceService",
    "is_end_phrase",
    "match_wake",
    "speech_text",
]
