# File: core/voice/recorder.py

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import numpy as np

from core.voice.errors import VoiceError

logger = logging.getLogger(__name__)


def default_stream_factory(sample_rate: int, device: int | str | None, callback: Callable[..., None]) -> Any:
    #! @allow-local-import
    import sounddevice

    return sounddevice.InputStream(samplerate=sample_rate, channels=1, dtype="float32", device=device, callback=callback)


class Recorder:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        device: int | str | None = None,
        max_seconds: float = 60.0,
        stream_factory: Callable[[int, int | str | None, Callable[..., None]], Any] | None = None,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.device = device
        self.max_seconds = float(max_seconds)
        self._stream_factory = stream_factory or default_stream_factory
        self._stream: Any = None
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._lock = threading.Lock()
        self.active = False

    @property
    def seconds(self) -> float:
        return self._frames / float(self.sample_rate)

    @property
    def full(self) -> bool:
        return self.seconds >= self.max_seconds

    def start(self) -> None:
        if self.active:
            return
        with self._lock:
            self._chunks = []
            self._frames = 0
        try:
            self._stream = self._stream_factory(self.sample_rate, self.device, self._on_audio)
            self._stream.start()
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self._stream = None
            raise VoiceError(f"The microphone could not be opened: {error}") from error
        self.active = True

    def _on_audio(self, indata: Any, frames: int, time_info: Any = None, status: Any = None) -> None:
        if status:
            logger.debug("Recorder status: %s", status)
        with self._lock:
            if self.full:
                return
            block = np.asarray(indata, dtype=np.float32)
            if block.ndim > 1:
                block = block[:, 0]
            self._chunks.append(block.copy())
            self._frames += int(block.shape[0])

    def stop(self) -> np.ndarray:
        if not self.active:
            return np.zeros(0, dtype=np.float32)
        stream, self._stream = self._stream, None
        self.active = False
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.debug("Recorder close failed: %s", error)
        with self._lock:
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32, copy=False)

    def cancel(self) -> None:
        self.stop()
