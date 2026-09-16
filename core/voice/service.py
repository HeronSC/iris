# File: core/voice/service.py

from __future__ import annotations

import importlib.util
import logging
import threading
from typing import Any

from core.voice.config import VoiceConfig
from core.voice.errors import VoiceError
from core.voice.recorder import Recorder
from core.voice.speaker import Speaker
from core.voice.text import speech_text
from core.voice.transcriber import Transcriber

logger = logging.getLogger(__name__)

REQUIRED_MODULES = ("sounddevice", "faster_whisper", "piper")


def missing_modules() -> list[str]:
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


class VoiceService:
    def __init__(
        self,
        config: VoiceConfig,
        *,
        recorder: Recorder | None = None,
        transcriber: Transcriber | None = None,
        speaker: Speaker | None = None,
        problem: str | None = None,
    ) -> None:
        self.config = config
        self.recorder = recorder
        self.transcriber = transcriber
        self.speaker = speaker
        self.problem = problem
        self.muted = False
        self._warm_thread: threading.Thread | None = None
        self.warm_error: str | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any], **overrides: Any) -> "VoiceService":
        settings = VoiceConfig.from_config(config)
        if not settings.enabled:
            return cls(settings, problem="Voice is turned off (voice.enabled is false).")
        missing = missing_modules()
        if missing and not overrides:
            return cls(settings, problem=f"Voice needs {', '.join(missing)} installed.")
        recorder = overrides.get("recorder") or Recorder(sample_rate=settings.sample_rate, device=settings.input_device, max_seconds=settings.max_seconds)
        transcriber = overrides.get("transcriber") or Transcriber(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            download_root=settings.models_path / "whisper",
            language=settings.language,
        )
        speaker = overrides.get("speaker") or Speaker(settings.piper_voice, settings.models_path, device=settings.output_device)
        return cls(settings, recorder=recorder, transcriber=transcriber, speaker=speaker)

    @property
    def available(self) -> bool:
        return self.problem is None and self.recorder is not None and self.transcriber is not None and self.speaker is not None

    @property
    def listening(self) -> bool:
        return bool(self.recorder is not None and self.recorder.active)

    @property
    def speaking(self) -> bool:
        return bool(self.speaker is not None and self.speaker.speaking)

    def warm_up(self, *, background: bool = True) -> None:
        if not self.available:
            return
        if background:
            if self._warm_thread is not None and self._warm_thread.is_alive():
                return
            self._warm_thread = threading.Thread(target=self._warm, name="iris-voice-warmup", daemon=True)
            self._warm_thread.start()
            return
        self._warm()

    def _warm(self) -> None:
        try:
            self.transcriber.load()
            self.speaker.load()
            self.warm_error = None
        except VoiceError as error:
            self.warm_error = str(error)
            logger.warning("Voice warm-up failed: %s", error)

    def start_listening(self) -> None:
        self._require()
        self.stop_speaking()
        self.recorder.start()

    def stop_listening(self) -> str:
        self._require()
        audio = self.recorder.stop()
        return self.transcriber.transcribe(audio, self.recorder.sample_rate)

    def cancel_listening(self) -> None:
        if self.recorder is not None:
            self.recorder.cancel()

    def speak(self, text: str, *, wait: bool = False) -> bool:
        if not self.available or self.muted:
            return False
        spoken = speech_text(text, self.config.max_spoken_chars)
        if not spoken:
            return False
        try:
            return self.speaker.speak(spoken, wait=wait)
        except VoiceError as error:
            logger.warning("Could not speak: %s", error)
            return False

    def stop_speaking(self) -> None:
        if self.speaker is not None:
            self.speaker.stop()

    def should_speak_reply(self, *, from_voice: bool) -> bool:
        if not self.available or self.muted:
            return False
        mode = self.config.speak_replies
        return mode == "always" or (mode == "voice" and from_voice)

    def describe(self) -> str:
        if not self.available:
            return self.problem or "Voice is not available."
        device = self.transcriber.device_used or self.config.whisper_device
        state = "muted" if self.muted else "on"
        return f"Voice {state}: hold {self.config.hotkey} to talk; Whisper {self.config.whisper_model} on {device}, Piper {self.config.piper_voice}."

    def shutdown(self) -> None:
        self.cancel_listening()
        self.stop_speaking()

    def _require(self) -> None:
        if not self.available:
            raise VoiceError(self.problem or "Voice is not available.")
