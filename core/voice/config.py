# File: core/voice/config.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TALK_HOTKEY = "ctrl+alt+t"
DEFAULT_WHISPER_MODEL = "small.en"
DEFAULT_PIPER_VOICE = "en_US-lessac-medium"
SPEAK_MODES = ("voice", "always", "never")


@dataclass(frozen=True)
class VoiceConfig:
    models_path: Path
    enabled: bool = True
    hotkey: str = DEFAULT_TALK_HOTKEY
    whisper_model: str = DEFAULT_WHISPER_MODEL
    whisper_device: str = "auto"
    whisper_compute_type: str = "default"
    language: str = "en"
    piper_voice: str = DEFAULT_PIPER_VOICE
    input_device: int | str | None = None
    output_device: int | str | None = None
    speak_replies: str = "voice"
    max_spoken_chars: int = 600
    max_seconds: float = 60.0
    sample_rate: int = 16000
    conversation: bool = True
    stream_speech: bool = True
    end_silence_ms: int = 700
    idle_seconds: float = 20.0
    max_utterance_seconds: float = 30.0
    answer_timeout_seconds: float = 10.0
    end_phrases: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "VoiceConfig":
        section = config.get("voice") if isinstance(config.get("voice"), dict) else {}
        memory_path = str(config.get("memory_path") or "")
        default_models = Path(memory_path).parent / "Models" if memory_path else Path("Models")
        speak = str(section.get("speak_replies") or "voice").strip().lower()
        if speak not in SPEAK_MODES:
            speak = "voice"
        return cls(
            models_path=Path(str(section.get("models_path") or default_models)),
            enabled=bool(section.get("enabled", True)),
            hotkey=str(section.get("hotkey") or DEFAULT_TALK_HOTKEY),
            whisper_model=str(section.get("whisper_model") or DEFAULT_WHISPER_MODEL),
            whisper_device=str(section.get("whisper_device") or "auto"),
            whisper_compute_type=str(section.get("whisper_compute_type") or "default"),
            language=str(section.get("language") or "en"),
            piper_voice=str(section.get("piper_voice") or DEFAULT_PIPER_VOICE),
            input_device=_device(section.get("input_device")),
            output_device=_device(section.get("output_device")),
            speak_replies=speak,
            max_spoken_chars=int(section.get("max_spoken_chars", 600)),
            max_seconds=float(section.get("max_seconds", 60.0)),
            sample_rate=int(section.get("sample_rate", 16000)),
            conversation=bool(section.get("conversation", True)),
            stream_speech=bool(section.get("stream_speech", True)),
            end_silence_ms=int(section.get("end_silence_ms", 700)),
            idle_seconds=float(section.get("idle_seconds", 20.0)),
            max_utterance_seconds=float(section.get("max_utterance_seconds", 30.0)),
            answer_timeout_seconds=float(section.get("answer_timeout_seconds", 10.0)),
            end_phrases=tuple(str(item) for item in section.get("end_phrases") or ()),
        )


def _device(value: Any) -> int | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return text or None
