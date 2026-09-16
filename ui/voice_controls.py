# File: ui/voice_controls.py

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from core.voice.errors import VoiceError

logger = logging.getLogger(__name__)

HOLD_THRESHOLD_SECONDS = 0.4
POLL_MS = 40


class TranscribeThread(QThread):
    resultSignal = Signal(str)
    failedSignal = Signal(str)

    def __init__(self, service: Any) -> None:
        super().__init__()
        self.service = service

    def run(self) -> None:
        try:
            self.resultSignal.emit(self.service.stop_listening())
        except VoiceError as error:
            self.failedSignal.emit(str(error))
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self.failedSignal.emit(f"Transcription failed: {error}")


class VoiceController(QObject):
    transcribed = Signal(str)
    stateChanged = Signal(str)
    failed = Signal(str)

    def __init__(self, service: Any, parent: QObject | None = None, *, key_is_down: Callable[[], bool] | None = None, clock: Callable[[], float] | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.key_is_down = key_is_down or (lambda: False)
        self.clock = clock or time.monotonic
        self.state = "idle"
        self.hold_mode = False
        self._pressed_at = 0.0
        self._worker: TranscribeThread | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)

    @property
    def listening(self) -> bool:
        return self.state == "listening"

    @property
    def busy(self) -> bool:
        return self.state in {"listening", "transcribing"}

    def on_hotkey(self) -> None:
        if not self.service.available:
            self.failed.emit(self.service.problem or "Voice is not available.")
            return
        if self.state == "transcribing":
            return
        if self.state == "listening":
            self.finish()
            return
        self.begin()

    def begin(self) -> None:
        try:
            self.service.start_listening()
        except VoiceError as error:
            self.failed.emit(str(error))
            return
        self._pressed_at = self.clock()
        self.hold_mode = False
        self._set_state("listening")
        self.timer.start()

    def poll(self) -> None:
        if self.state != "listening":
            self.timer.stop()
            return
        if self.service.recorder is not None and self.service.recorder.full:
            self.finish()
            return
        if self.key_is_down():
            return
        held = self.clock() - self._pressed_at
        if held >= HOLD_THRESHOLD_SECONDS:
            self.hold_mode = True
            self.finish()
            return
        self.timer.stop()

    def finish(self) -> None:
        self.timer.stop()
        if self.state != "listening":
            return
        self._set_state("transcribing")
        worker = TranscribeThread(self.service)
        worker.resultSignal.connect(self._on_result)
        worker.failedSignal.connect(self._on_failed)
        worker.finished.connect(self._on_worker_done)
        self._worker = worker
        worker.start()

    def cancel(self) -> None:
        self.timer.stop()
        if self.state == "listening":
            self.service.cancel_listening()
        self._set_state("idle")

    def stop_speaking(self) -> bool:
        if self.service.speaking:
            self.service.stop_speaking()
            return True
        return False

    def _on_result(self, text: str) -> None:
        self._set_state("idle")
        cleaned = (text or "").strip()
        if not cleaned:
            self.failed.emit("Iris did not hear anything.")
            return
        self.transcribed.emit(cleaned)

    def _on_failed(self, error_text: str) -> None:
        self._set_state("idle")
        self.failed.emit(error_text)

    def _on_worker_done(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.deleteLater()
        self._worker = None

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.stateChanged.emit(state)
