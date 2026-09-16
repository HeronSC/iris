# File: core/application/instance_lock.py

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import IO, Callable

import structlog

logger = structlog.get_logger(__name__)

if sys.platform == "win32":
    #! @allow-local-import
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            return

else:
    #! @allow-local-import
    import fcntl

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(
        self,
        *,
        timeout: float,
        poll_seconds: float = 0.5,
        on_wait: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
        except OSError:
            pass
        deadline = time.monotonic() + max(0.0, timeout)
        waited = False
        while True:
            if _try_lock(handle):
                break
            if cancel is not None and cancel.is_set():
                handle.close()
                return False
            now = time.monotonic()
            if now >= deadline:
                handle.close()
                logger.warning("Instance lock not released in time; continuing", path=str(self.path))
                return False
            if not waited:
                logger.info("Waiting for the previous Iris instance to finish", path=str(self.path))
                waited = True
            if on_wait is not None:
                on_wait(deadline - now)
            time.sleep(poll_seconds)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
        except OSError:
            pass
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        _unlock(handle)
        try:
            handle.close()
        except OSError:
            pass


__all__ = ["InstanceLock"]
