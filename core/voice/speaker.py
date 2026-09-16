# File: core/voice/speaker.py

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

from core.voice.errors import VoiceError

logger = logging.getLogger(__name__)

BLOCK_SAMPLES = 2048


def default_voice_loader(model_path: Path) -> Any:
    #! @allow-local-import
    from piper import PiperVoice

    return PiperVoice.load(model_path)


def default_downloader(voice_name: str, folder: Path) -> None:
    #! @allow-local-import
    from piper.download_voices import download_voice

    download_voice(voice_name, folder)


def default_player_factory(sample_rate: int, device: int | str | None) -> Any:
    #! @allow-local-import
    import sounddevice

    stream = sounddevice.OutputStream(samplerate=sample_rate, channels=1, dtype="int16", device=device)
    stream.start()
    return stream


class Speaker:
    def __init__(
        self,
        voice_name: str,
        models_path: Path,
        *,
        device: int | str | None = None,
        voice_loader: Callable[[Path], Any] | None = None,
        downloader: Callable[[str, Path], None] | None = None,
        player_factory: Callable[[int, int | str | None], Any] | None = None,
    ) -> None:
        self.voice_name = voice_name
        self.models_path = Path(models_path)
        self.device = device
        self._loader = voice_loader or default_voice_loader
        self._downloader = downloader or default_downloader
        self._player_factory = player_factory or default_player_factory
        self._voice: Any = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def model_path(self) -> Path:
        return self.models_path / "piper" / f"{self.voice_name}.onnx"

    @property
    def loaded(self) -> bool:
        return self._voice is not None

    @property
    def speaking(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def load(self) -> Any:
        with self._lock:
            if self._voice is not None:
                return self._voice
            path = self.model_path
            if not path.exists():
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._downloader(self.voice_name, path.parent)
                except (OSError, ValueError, RuntimeError, TypeError) as error:
                    raise VoiceError(f"Piper voice {self.voice_name} is not available: {error}") from error
            try:
                self._voice = self._loader(path)
            except (OSError, ValueError, RuntimeError, TypeError, ImportError) as error:
                raise VoiceError(f"Piper voice {self.voice_name} could not be loaded: {error}") from error
            return self._voice

    def speak(self, text: str, *, wait: bool = False) -> bool:
        spoken = (text or "").strip()
        if not spoken:
            return False
        self.stop()
        voice = self.load()
        self._stop.clear()
        thread = threading.Thread(target=self._run, args=(voice, spoken), name="iris-speaker", daemon=True)
        self._thread = thread
        thread.start()
        if wait:
            thread.join()
        return True

    def stop(self) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._stop.set()
        thread.join(timeout=2.0)

    def _run(self, voice: Any, text: str) -> None:
        player: Any = None
        self.last_error = None
        try:
            for chunk in voice.synthesize(text):
                if self._stop.is_set():
                    break
                rate = int(getattr(chunk, "sample_rate", 0) or getattr(getattr(voice, "config", None), "sample_rate", 22050))
                if player is None:
                    player = self._player_factory(rate, self.device)
                samples = np.asarray(chunk.audio_int16_array, dtype=np.int16).reshape(-1)
                for start in range(0, samples.shape[0], BLOCK_SAMPLES):
                    if self._stop.is_set():
                        break
                    player.write(samples[start : start + BLOCK_SAMPLES].reshape(-1, 1))
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self.last_error = str(error)
            logger.warning("Speech playback failed: %s", error)
        finally:
            if player is not None:
                try:
                    if self._stop.is_set() and hasattr(player, "abort"):
                        player.abort()
                    player.close()
                except (OSError, ValueError, RuntimeError, TypeError):
                    pass
