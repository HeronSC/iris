# File: core/documents/watch.py

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.documents.models import coerce_document_search_root
from core.documents.scanner import DocumentScanner

logger = logging.getLogger(__name__)


class DocumentWatchService:
    def __init__(
        self,
        scanner: DocumentScanner,
        *,
        debounce_seconds: float = 2.0,
        rescan_interval_hours: float = 24.0,
    ) -> None:
        self.scanner = scanner
        self.debounce_seconds = max(0.05, float(debounce_seconds))
        self.rescan_interval_hours = float(rescan_interval_hours)
        self._pending: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._observer: Any = None
        self._watched_roots: list[Path] = []
        self._started_at: float | None = None
        self._last_rescan: float | None = None
        self.stats: dict[str, Any] = {"events": 0, "indexed": 0, "updated": 0, "removed": 0, "ignored": 0, "errors": 0, "last_change": None, "last_error": None}


    @staticmethod
    def available() -> bool:
        try:
            #! @allow-local-import
            import watchdog.observers
        except ImportError:
            return False
        return True

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._started_at = time.monotonic()
        self._worker = threading.Thread(target=self._run, name="document-watch", daemon=True)
        self._worker.start()
        self.refresh_roots()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        observer = self._observer
        self._observer = None
        if observer is not None:
            try:
                observer.stop()
                observer.join(5)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Stopping the document watcher: %s", error)
        if self._worker is not None:
            self._worker.join(5)
            self._worker = None

    @property
    def running(self) -> bool:
        return self._observer is not None and self._worker is not None

    def refresh_roots(self) -> None:
        if self._worker is None:
            return
        #! @allow-local-import
        from watchdog.observers import Observer

        roots: list[Path] = []
        for root in self.scanner.config.roots:
            path = coerce_document_search_root(root).path
            if path.exists() and path.is_dir():
                roots.append(path)
        if roots == self._watched_roots and self._observer is not None:
            return
        old = self._observer
        observer = Observer()
        handler = _Handler(self)
        for path in roots:
            try:
                observer.schedule(handler, str(path), recursive=True)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Cannot watch %s: %s", path, error)
        observer.daemon = True
        observer.start()
        self._observer = observer
        self._watched_roots = roots
        if old is not None:
            try:
                old.stop()
                old.join(5)
            except (OSError, ValueError, RuntimeError, TypeError):
                pass
        logger.info("Watching %d document root(s) for changes", len(roots))


    def enqueue(self, path: str, action: str) -> None:
        with self._lock:
            self.stats["events"] += 1
            self._pending.pop(path, None)
            self._pending[path] = (action, time.monotonic())
        self._wake.set()

    def flush(self) -> dict[str, int]:
        return self._drain(force=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            self._drain(force=False)
            self._maybe_rescan()

    def _drain(self, *, force: bool) -> dict[str, int]:
        applied = {"indexed": 0, "updated": 0, "removed": 0, "ignored": 0, "errors": 0}
        while True:
            now = time.monotonic()
            with self._lock:
                ready = [path for path, (_, seen) in self._pending.items() if force or now - seen >= self.debounce_seconds]
                if not ready:
                    if self._pending and not force:
                        self._wake.set()
                        time.sleep(min(self.debounce_seconds, 0.25))
                        continue
                    return applied
                batch = [(path, self._pending.pop(path)[0]) for path in ready]
            for path, action in batch:
                outcome = self._apply(Path(path), action)
                applied[outcome] = applied.get(outcome, 0) + 1
                self.stats[outcome] = self.stats.get(outcome, 0) + 1
            self.stats["last_change"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _apply(self, path: Path, action: str) -> str:
        try:
            if action == "deleted" or not path.exists():
                removed = self.scanner.remove_path(path)
                return "removed" if removed else "ignored"
            if path.is_dir():
                count = 0
                for child in path.rglob("*"):
                    if child.is_file() and self.scanner.is_indexable(child):
                        result = self.scanner.index_file(child)
                        count += result in {"indexed", "updated"}
                return "indexed" if count else "ignored"
            result = self.scanner.index_file(path)
            if result in {"indexed", "updated"}:
                return result
            if result == "error":
                return "errors"
            return "ignored"
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self.stats["last_error"] = f"{path}: {error}"
            logger.warning("Change to %s could not be applied: %s", path, error)
            return "errors"

    def _maybe_rescan(self) -> None:
        if self.rescan_interval_hours <= 0 or self._started_at is None:
            return
        interval = self.rescan_interval_hours * 3600
        reference = self._last_rescan if self._last_rescan is not None else self._started_at
        if time.monotonic() - reference < interval:
            return
        self._last_rescan = time.monotonic()
        try:
            result = self.scanner.scan()
            logger.info("Safety-net rescan: %s", result)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self.stats["last_error"] = f"rescan: {error}"
            logger.warning("Safety-net rescan failed: %s", error)


    def status(self) -> dict[str, Any]:
        with self._lock:
            pending = len(self._pending)
        return {
            "running": self.running,
            "roots": [str(path) for path in self._watched_roots],
            "pending": pending,
            "debounce_seconds": self.debounce_seconds,
            "rescan_interval_hours": self.rescan_interval_hours,
            **self.stats,
        }


class _Handler:

    def __init__(self, service: DocumentWatchService) -> None:
        self.service = service

    def dispatch(self, event: Any) -> None:
        kind = getattr(event, "event_type", "")
        if kind == "moved":
            self.service.enqueue(str(getattr(event, "src_path", "")), "deleted")
            self.service.enqueue(str(getattr(event, "dest_path", "")), "changed")
            return
        if kind in {"created", "modified", "closed"}:
            if getattr(event, "is_directory", False) and kind == "modified":
                return
            self.service.enqueue(str(getattr(event, "src_path", "")), "changed")
        elif kind == "deleted":
            self.service.enqueue(str(getattr(event, "src_path", "")), "deleted")
