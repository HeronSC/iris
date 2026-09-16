# File: core/voice/transcriber.py

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

from core.voice.errors import VoiceError

logger = logging.getLogger(__name__)

MIN_SECONDS = 0.3


def default_model_factory(model_name: str, device: str, compute_type: str, download_root: Path | None) -> Any:
    #! @allow-local-import
    from faster_whisper import WhisperModel

    return WhisperModel(model_name, device=device, compute_type=compute_type, download_root=str(download_root) if download_root else None)


class Transcriber:
    def __init__(
        self,
        model_name: str,
        *,
        device: str = "auto",
        compute_type: str = "default",
        download_root: Path | None = None,
        language: str = "en",
        model_factory: Callable[[str, str, str, Path | None], Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.language = language
        self._factory = model_factory or default_model_factory
        self._model: Any = None
        self._lock = threading.Lock()
        self.device_used: str | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                self._model = self._probe(self.device, self.compute_type)
                self.device_used = self.device
            except (OSError, ValueError, RuntimeError, TypeError, ImportError) as error:
                if self.device == "cpu":
                    raise VoiceError(f"Whisper model {self.model_name} could not be loaded: {error}") from error
                logger.warning("Whisper on %s failed (%s); falling back to CPU", self.device, error)
                try:
                    self._model = self._probe("cpu", "int8")
                    self.device_used = "cpu"
                except (OSError, ValueError, RuntimeError, TypeError, ImportError) as cpu_error:
                    raise VoiceError(f"Whisper model {self.model_name} could not be loaded: {cpu_error}") from cpu_error
            return self._model

    def _probe(self, device: str, compute_type: str) -> Any:
        model = self._factory(self.model_name, device, compute_type, self.download_root)
        silence = np.zeros(16000, dtype=np.float32)
        segments, _info = model.transcribe(silence, language=self.language or None, beam_size=1)
        for _segment in segments:
            break
        return model

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.shape[0] < int(MIN_SECONDS * sample_rate):
            return ""
        if sample_rate != 16000:
            samples = _resample(samples, sample_rate, 16000)
        model = self.load()
        try:
            segments, _info = model.transcribe(samples, language=self.language or None, beam_size=5, vad_filter=True)
            text = " ".join(str(getattr(segment, "text", "")).strip() for segment in segments)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            raise VoiceError(f"Transcription failed: {error}") from error
        return " ".join(text.split()).strip()


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if samples.shape[0] == 0 or source_rate == target_rate:
        return samples
    length = int(round(samples.shape[0] * target_rate / float(source_rate)))
    positions = np.linspace(0, samples.shape[0] - 1, num=length)
    return np.interp(positions, np.arange(samples.shape[0]), samples).astype(np.float32)
