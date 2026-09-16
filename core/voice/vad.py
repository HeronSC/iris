# File: core/voice/vad.py

from __future__ import annotations

from typing import Any, Callable

import numpy as np

FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
STATE_SHAPE = (1, 1, 128)


def default_session_factory() -> Any:
    #! @allow-local-import
    from faster_whisper.vad import get_vad_model

    return get_vad_model().session


class StreamingVad:
    def __init__(self, session_factory: Callable[[], Any] | None = None) -> None:
        self._factory = session_factory or default_session_factory
        self._session: Any = None
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        samples = np.asarray(frame, dtype=np.float32).reshape(-1)
        if samples.shape[0] != FRAME_SAMPLES:
            padded = np.zeros(FRAME_SAMPLES, dtype=np.float32)
            padded[: min(FRAME_SAMPLES, samples.shape[0])] = samples[:FRAME_SAMPLES]
            samples = padded
        if self._session is None:
            self._session = self._factory()
        batch = np.concatenate([self._context, samples]).reshape(1, -1)
        out, self._h, self._c = self._session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self._context = samples[-CONTEXT_SAMPLES:]
        return float(np.asarray(out).reshape(-1)[0])
