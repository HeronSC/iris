# File: core/voice/service.py

from __future__ import annotations

import importlib.util
import logging
import threading
from typing import Any, Callable

from core.voice.config import VoiceConfig
from core.voice.errors import VoiceError
from core.voice.recorder import Recorder
from core.voice.session import DEFAULT_END_PHRASES, DEFAULT_WAKE_NAMES, ConversationSession
from core.voice.speaker import Speaker
from core.voice.stream import SentenceStreamer
from core.voice.text import speech_text
from core.voice.transcriber import Transcriber
from core.voice.vad import StreamingVad

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
        self.on_utterance: Callable[[str], None] | None = None
        self.on_conversation_state: Callable[[str], None] | None = None
        self.on_conversation_end: Callable[[str], None] | None = None
        self.on_wake: Callable[[str], None] | None = None
        self.session_factory: Callable[..., ConversationSession] = ConversationSession
        self.vad_factory: Callable[[], StreamingVad] = StreamingVad
        self.conversation: ConversationSession | None = None

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

    def speak_sentence(self, text: str) -> bool:
        if not self.available or self.muted:
            return False
        spoken = (text or "").strip()
        if not spoken:
            return False
        try:
            return self.speaker.enqueue(spoken)
        except VoiceError as error:
            logger.warning("Could not speak: %s", error)
            return False

    def sentence_streamer(self) -> SentenceStreamer:
        return SentenceStreamer(self.speak_sentence, limit=self.config.max_spoken_chars)

    def stop_speaking(self) -> None:
        if self.speaker is not None:
            self.speaker.stop()

    def should_speak_reply(self, *, from_voice: bool) -> bool:
        if not self.available or self.muted:
            return False
        mode = self.config.speak_replies
        return mode == "always" or (mode == "voice" and from_voice)

    @property
    def in_conversation(self) -> bool:
        session = self.conversation
        return session is not None and session.active

    @property
    def wake_mode(self) -> bool:
        session = self.conversation
        return session is not None and session.active and session.wake_mode

    @property
    def asleep(self) -> bool:
        session = self.conversation
        return session is not None and session.active and session.asleep

    def start_conversation(self, *, wake: bool = False) -> ConversationSession:
        self._require()
        if self.on_utterance is None:
            raise VoiceError("No voice client is attached to receive what is said.")
        current = self.conversation
        if current is not None and current.active:
            if current.wake_mode == wake:
                return current
            current.stop("stopped")
        self.cancel_listening()
        settings = self.config
        session = self.session_factory(
            self,
            on_utterance=self.on_utterance,
            on_state=self.on_conversation_state,
            on_end=self._conversation_ended,
            on_wake=self._woke,
            vad=self.vad_factory(),
            sample_rate=settings.sample_rate,
            device=settings.input_device,
            end_silence_ms=settings.end_silence_ms,
            idle_seconds=settings.idle_seconds,
            max_utterance_seconds=settings.max_utterance_seconds,
            end_phrases=settings.end_phrases or DEFAULT_END_PHRASES,
            wake_names=(settings.wake_names or DEFAULT_WAKE_NAMES) if wake else (),
        )
        self.conversation = session
        session.start()
        return session

    def start_wake(self) -> ConversationSession:
        return self.start_conversation(wake=True)

    def stop_wake(self) -> bool:
        if not self.wake_mode:
            return False
        return self.end_conversation("wake-off")

    def wake_now(self) -> bool:
        session = self.conversation
        if session is None or not session.active or not session.wake_mode:
            return False
        if session.asleep:
            session.wake("")
        else:
            session.sleep()
        return True

    def _woke(self, command: str) -> None:
        handler = self.on_wake
        if handler is not None:
            handler(command)

    def end_conversation(self, reason: str = "stopped") -> bool:
        session = self.conversation
        if session is None or not session.active:
            return False
        session.stop(reason)
        return True

    def _conversation_ended(self, reason: str) -> None:
        handler = self.on_conversation_end
        if handler is not None:
            handler(reason)

    def hold_conversation(self) -> None:
        session = self.conversation
        if session is not None and session.active:
            session.hold()

    def resume_conversation(self) -> None:
        session = self.conversation
        if session is not None and session.active:
            session.resume()

    def ask(self, question: str, *, timeout: float | None = None) -> str | None:
        if not self.available:
            return None
        wait = float(timeout if timeout is not None else self.config.answer_timeout_seconds)
        session = self.conversation
        if session is None or not session.active:
            return None
        self.speak(question, wait=True)
        return session.request_answer(timeout=wait)

    def describe(self) -> str:
        if not self.available:
            return self.problem or "Voice is not available."
        device = self.transcriber.device_used or self.config.whisper_device
        state = "muted" if self.muted else "on"
        if self.wake_mode:
            names = ", ".join(self.config.wake_names or DEFAULT_WAKE_NAMES[:2])
            mode = f"{'listening for your name' if self.asleep else 'awake'} ({names}); {self.config.wake_hotkey} turns the wake word off"
        elif self.in_conversation:
            mode = f"in a conversation: {self.config.hotkey}"
        else:
            mode = f"{'tap to start a conversation, hold to talk once' if self.config.conversation else 'hold to talk'}: {self.config.hotkey}; {self.config.wake_hotkey} turns the wake word on"
        return f"Voice {state}, {mode}; Whisper {self.config.whisper_model} on {device}, Piper {self.config.piper_voice}."

    def shutdown(self) -> None:
        self.end_conversation("shutdown")
        self.cancel_listening()
        self.stop_speaking()

    def _require(self) -> None:
        if not self.available:
            raise VoiceError(self.problem or "Voice is not available.")
