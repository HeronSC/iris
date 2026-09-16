# File: core/voice/session.py

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from core.voice.errors import VoiceError
from core.voice.text import normalize_phrase
from core.voice.vad import FRAME_SAMPLES, StreamingVad

logger = logging.getLogger(__name__)

DEFAULT_END_PHRASES = ("that's all", "that is all", "that's it", "thanks iris", "thank you iris", "goodbye", "good bye", "stop listening", "end conversation")
START_FRAMES = 3
BARGE_IN_FRAMES = 8
PRE_ROLL_FRAMES = 10


def default_input_factory(sample_rate: int, device: int | str | None, callback: Callable[..., None]) -> Any:
    #! @allow-local-import
    import sounddevice

    return sounddevice.InputStream(samplerate=sample_rate, channels=1, dtype="float32", device=device, blocksize=FRAME_SAMPLES, callback=callback)


def is_end_phrase(text: str, phrases: tuple[str, ...] = DEFAULT_END_PHRASES) -> bool:
    spoken = normalize_phrase(text)
    if not spoken:
        return False
    for phrase in phrases:
        wanted = normalize_phrase(phrase)
        if not wanted:
            continue
        if spoken == wanted or spoken.endswith(" " + wanted):
            return True
        if spoken.startswith(wanted + " ") and len(spoken) <= len(wanted) + 12:
            return True
    return False


class ConversationSession:
    def __init__(
        self,
        service: Any,
        *,
        on_utterance: Callable[[str], None],
        on_state: Callable[[str], None] | None = None,
        on_end: Callable[[str], None] | None = None,
        vad: StreamingVad | None = None,
        input_factory: Callable[[int, int | str | None, Callable[..., None]], Any] | None = None,
        clock: Callable[[], float] | None = None,
        sample_rate: int = 16000,
        device: int | str | None = None,
        end_silence_ms: int = 700,
        idle_seconds: float = 20.0,
        max_utterance_seconds: float = 30.0,
        threshold: float = 0.5,
        end_phrases: tuple[str, ...] = DEFAULT_END_PHRASES,
    ) -> None:
        self.service = service
        self.on_utterance = on_utterance
        self.on_state = on_state or (lambda state: None)
        self.on_end = on_end or (lambda reason: None)
        self.vad = vad or StreamingVad()
        self._input_factory = input_factory or default_input_factory
        self.clock = clock or time.monotonic
        self.sample_rate = int(sample_rate)
        self.device = device
        self.end_silence_frames = max(1, int(end_silence_ms * sample_rate / 1000 / FRAME_SAMPLES))
        self.idle_seconds = float(idle_seconds)
        self.max_utterance_frames = max(1, int(max_utterance_seconds * sample_rate / FRAME_SAMPLES))
        self.threshold = float(threshold)
        self.end_phrases = tuple(end_phrases)
        self._frames: queue.Queue[np.ndarray | None] = queue.Queue()
        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._held = threading.Event()
        self._answer_wanted = threading.Event()
        self._answer: queue.Queue[str] = queue.Queue()
        self.state = "idle"
        self.end_reason: str | None = None
        self._last_activity = 0.0

    @property
    def active(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        if self.active:
            return
        self._stop.clear()
        self._held.clear()
        self.end_reason = None
        self.vad.reset()
        try:
            self._stream = self._input_factory(self.sample_rate, self.device, self._on_audio)
            self._stream.start()
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self._stream = None
            raise VoiceError(f"The microphone could not be opened: {error}") from error
        self._last_activity = self.clock()
        self._thread = threading.Thread(target=self._run, name="iris-conversation", daemon=True)
        self._thread.start()
        self._set_state("waiting")

    def stop(self, reason: str = "stopped") -> None:
        if self._thread is None:
            return
        if self.end_reason is None:
            self.end_reason = reason
        self._stop.set()
        self._frames.put(None)
        thread = self._thread
        if thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._close_stream()
        self._thread = None
        self._set_state("idle")

    def hold(self) -> None:
        self._held.set()
        self._last_activity = self.clock()

    def resume(self) -> None:
        self._held.clear()
        self._last_activity = self.clock()

    def request_answer(self, timeout: float = 10.0) -> str | None:
        while not self._answer.empty():
            self._answer.get_nowait()
        self._answer_wanted.set()
        self._held.clear()
        try:
            return self._answer.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self._answer_wanted.clear()

    def _on_audio(self, indata: Any, frames: int, time_info: Any = None, status: Any = None) -> None:
        block = np.asarray(indata, dtype=np.float32)
        if block.ndim > 1:
            block = block[:, 0]
        self._frames.put(block.copy())

    def feed(self, frame: np.ndarray) -> None:
        self._frames.put(np.asarray(frame, dtype=np.float32).reshape(-1))

    def _run(self) -> None:
        pre_roll: list[np.ndarray] = []
        speech: list[np.ndarray] = []
        speech_run = 0
        silence_run = 0
        try:
            while not self._stop.is_set():
                try:
                    frame = self._frames.get(timeout=0.25)
                except queue.Empty:
                    self._check_idle()
                    continue
                if frame is None:
                    break
                voiced = self.vad.probability(frame) >= self.threshold
                if self.state == "waiting":
                    pre_roll.append(frame)
                    del pre_roll[:-PRE_ROLL_FRAMES]
                    speech_run = speech_run + 1 if voiced else 0
                    if self.service.speaking:
                        self._last_activity = self.clock()
                        if speech_run >= BARGE_IN_FRAMES:
                            self.service.stop_speaking()
                            speech = list(pre_roll)
                            silence_run = 0
                            self._set_state("speaking")
                    elif speech_run >= START_FRAMES:
                        speech = list(pre_roll)
                        silence_run = 0
                        self._set_state("speaking")
                    else:
                        self._check_idle()
                    continue
                if self.state == "speaking":
                    speech.append(frame)
                    silence_run = 0 if voiced else silence_run + 1
                    if silence_run >= self.end_silence_frames or len(speech) >= self.max_utterance_frames:
                        audio = np.concatenate(speech) if speech else np.zeros(0, dtype=np.float32)
                        speech = []
                        pre_roll = []
                        speech_run = 0
                        self._finish_utterance(audio)
                        if self._stop.is_set():
                            break
                        self._set_state("waiting")
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.warning("Conversation session stopped: %s", error)
            self.end_reason = self.end_reason or f"error: {error}"
            self._stop.set()
        finally:
            self._close_stream()
            reason = self.end_reason or "stopped"
            try:
                self.on_end(reason)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.debug("Conversation end handler failed: %s", error)

    def _finish_utterance(self, audio: np.ndarray) -> None:
        self._set_state("transcribing")
        try:
            text = self.service.transcriber.transcribe(audio, self.sample_rate)
        except VoiceError as error:
            logger.warning("Conversation transcription failed: %s", error)
            text = ""
        self._last_activity = self.clock()
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if self._answer_wanted.is_set():
            self._answer.put(cleaned)
            return
        if is_end_phrase(cleaned, self.end_phrases):
            self.end_reason = "phrase"
            self._stop.set()
            return
        if self._held.is_set():
            logger.debug("Dropping utterance while a reply is in progress: %s", cleaned)
            return
        try:
            self.on_utterance(cleaned)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.warning("Utterance handler failed: %s", error)

    def _check_idle(self) -> None:
        if self._held.is_set() or self.service.speaking or self._answer_wanted.is_set():
            self._last_activity = self.clock()
            return
        if self.clock() - self._last_activity >= self.idle_seconds:
            self.end_reason = "idle"
            self._stop.set()

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.debug("Conversation stream close failed: %s", error)

    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        try:
            self.on_state(state)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.debug("Conversation state handler failed: %s", error)
