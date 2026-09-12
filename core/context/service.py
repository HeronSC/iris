# File: core/context/service.py

from __future__ import annotations

import logging
import os
import re
import threading
from collections import deque
from typing import Any, Callable, Sequence

from core.context.models import ActiveContext, WindowInfo
from core.context.providers import ContextProvider

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 1.5
DEFAULT_HISTORY = 12

THIS_WORDS = re.compile(r"\b(this|here|current|open|active|the (?:file|workbook|document|folder|window|project) i have open)\b", re.IGNORECASE)
PREVIOUS_WORDS = re.compile(r"\b(previous|before|earlier|last|other)\b", re.IGNORECASE)


class ContextService:
    def __init__(
        self,
        providers: Sequence[ContextProvider],
        *,
        foreground: Callable[[], WindowInfo | None],
        own_pid: int | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        history_size: int = DEFAULT_HISTORY,
        audit: Any = None,
    ) -> None:
        self.providers = tuple(providers)
        self.foreground = foreground
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.audit = audit
        self._history: deque[ActiveContext] = deque(maxlen=max(1, int(history_size)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.paused = False
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="iris-context", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.poll_seconds + 1.0)
        self._thread = None

    def pause(self) -> None:
        self.paused = True
        self._audit("context.paused", "Context capture paused")

    def resume(self) -> None:
        self.paused = False
        self._audit("context.resumed", "Context capture resumed")

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            if self.paused:
                continue
            try:
                self.sample()
            except Exception as error:
                self.last_error = str(error)
                logger.debug("Context sample failed: %s", error)

    def sample(self) -> ActiveContext | None:
        window = self.foreground()
        if window is None or window.pid == self.own_pid:
            return None
        captured = self.capture_window(window)
        if captured is None:
            return None
        with self._lock:
            latest = self._history[-1] if self._history else None
            if latest is not None and latest.key == captured.key:
                return latest
            self._history.append(captured)
        return captured

    def capture_window(self, window: WindowInfo) -> ActiveContext | None:
        for provider in self.providers:
            try:
                if not provider.matches(window):
                    continue
                captured = provider.capture(window)
            except Exception as error:
                self.last_error = f"{provider.name}: {error}"
                logger.debug("Context provider %s failed: %s", provider.name, error)
                continue
            if captured is not None:
                return captured
        return None

    def current(self) -> ActiveContext | None:
        if not self.paused:
            try:
                self.sample()
            except Exception as error:
                self.last_error = str(error)
        with self._lock:
            return self._history[-1] if self._history else None

    def previous(self) -> ActiveContext | None:
        with self._lock:
            return self._history[-2] if len(self._history) > 1 else None

    def history(self) -> list[ActiveContext]:
        with self._lock:
            return list(reversed(self._history))

    def resolve(self, reference: str) -> tuple[ActiveContext | None, str]:
        text = (reference or "").strip()
        if PREVIOUS_WORDS.search(text):
            picked = self.previous()
            return picked, "the one before it" if picked else "nothing before the current window"
        picked = self.current()
        if picked is None:
            return None, "no window has been seen yet" if not self.paused else "context capture is paused"
        return picked, picked.describe()

    def prompt_line(self) -> str:
        if self.paused:
            return ""
        current = self.current()
        if current is None:
            return ""
        previous = self.previous()
        line = f"- Now: {current.describe()}"
        if previous is not None:
            line += f"\n- Before that: {previous.describe()}"
        return line

    def _audit(self, event: str, message: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(event, message)
        except Exception as error:
            logger.debug("Context audit failed: %s", error)


__all__ = ["ContextService", "DEFAULT_HISTORY", "DEFAULT_POLL_SECONDS", "THIS_WORDS"]
