# File: core/documents/roots.py

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_PROBE_SECONDS = 2.0


@dataclass(frozen=True)
class RootStatus:
    path: Path
    online: bool
    reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.path} ({'online' if self.online else 'offline: ' + self.reason})"


def probe_root(path: str | Path, *, timeout: float = DEFAULT_PROBE_SECONDS) -> RootStatus:
    target = Path(path)
    outcome: dict[str, object] = {}

    def check() -> None:
        try:
            outcome["exists"] = target.is_dir()
        except OSError as error:
            outcome["error"] = str(error)

    worker = threading.Thread(target=check, name="root-probe", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return RootStatus(path=target, online=False, reason=f"did not answer within {timeout:g} s")
    if "error" in outcome:
        return RootStatus(path=target, online=False, reason=str(outcome["error"]))
    if not outcome.get("exists"):
        return RootStatus(path=target, online=False, reason="not found or not a folder")
    return RootStatus(path=target, online=True)


def probe_roots(paths: Iterable[str | Path], *, timeout: float = DEFAULT_PROBE_SECONDS) -> list[RootStatus]:
    return [probe_root(path, timeout=timeout) for path in paths]


def is_unc(path: str | Path) -> bool:
    text = str(path)
    return text.startswith("\\\\") or text.startswith("//")


__all__ = ["DEFAULT_PROBE_SECONDS", "RootStatus", "is_unc", "probe_root", "probe_roots"]
