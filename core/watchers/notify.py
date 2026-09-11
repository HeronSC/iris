# File: core/watchers/notify.py

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Protocol

from core.watchers.models import Notification

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    name: str

    def send(self, notification: Notification) -> None:
        ...


class ToastNotifier:

    name = "toast"

    def __init__(self, application_name: str = "Iris") -> None:
        self.application_name = application_name
        self._toaster: Any = None

    @staticmethod
    def available() -> bool:
        try:
            #! @allow-local-import
            import windows_toasts
        except Exception:
            return False
        return True

    def send(self, notification: Notification) -> None:
        #! @allow-local-import
        from windows_toasts import Toast, WindowsToaster

        if self._toaster is None:
            self._toaster = WindowsToaster(self.application_name)
        toast = Toast([notification.title, notification.body[:400]])
        toast.group = "iris-watchers"
        self._toaster.show_toast(toast)


class LogNotifier:
    name = "log"

    def send(self, notification: Notification) -> None:
        logger.warning("watcher: %s — %s", notification.title, notification.body)


class InboxNotifier:

    name = "inbox"

    def __init__(self, path: str | Path | None = None, *, keep: int = 200) -> None:
        self.path = Path(path) if path else None
        self.keep = keep
        self._items: list[Notification] = []
        self._lock = threading.Lock()
        if self.path is not None:
            self._load()

    def send(self, notification: Notification) -> None:
        with self._lock:
            self._items.append(notification)
            self._items = self._items[-self.keep :]
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(notification.to_json(), ensure_ascii=False) + "\n")
                except OSError as error:
                    logger.warning("Could not append to the notification inbox: %s", error)

    def recent(self, limit: int = 20) -> list[Notification]:
        with self._lock:
            return list(self._items[-limit:])[::-1]

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-self.keep :]
        except OSError:
            return
        for line in lines:
            try:
                payload = json.loads(line)
                payload["delivered_to"] = tuple(payload.get("delivered_to") or ())
                self._items.append(Notification(**payload))
            except (ValueError, TypeError):
                continue


class QuietHours:

    def __init__(self, start: dtime | None = None, end: dtime | None = None) -> None:
        self.start = start
        self.end = end

    @classmethod
    def parse(cls, text: str | None) -> "QuietHours":
        if not text or not str(text).strip():
            return cls()
        try:
            start_text, end_text = str(text).replace(" ", "").split("-", 1)
            return cls(dtime.fromisoformat(start_text), dtime.fromisoformat(end_text))
        except ValueError as error:
            raise ValueError(f"Quiet hours must look like 22:00-07:00, not {text!r}") from error

    @property
    def configured(self) -> bool:
        return self.start is not None and self.end is not None

    def active(self, now: datetime | None = None) -> bool:
        if not self.configured:
            return False
        moment = (now or datetime.now()).time()
        assert self.start is not None and self.end is not None
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end

    def __str__(self) -> str:
        if not self.configured:
            return "off"
        return f"{self.start:%H:%M}-{self.end:%H:%M}"
