# File: core/system/limits.py

from __future__ import annotations

from typing import Any

try:
    import psutil
except ImportError:
    psutil = None

PRIORITY_NAMES = ("idle", "below_normal", "normal", "above_normal", "high")


def apply_process_priority(name: str | None, *, process: Any = None) -> str | None:
    wanted = str(name or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not wanted or wanted == "normal":
        return None
    if wanted not in PRIORITY_NAMES:
        raise ValueError(f"priority is one of {', '.join(PRIORITY_NAMES)}")
    if psutil is None:
        return None
    levels = {
        "idle": psutil.IDLE_PRIORITY_CLASS,
        "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
        "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
        "high": psutil.HIGH_PRIORITY_CLASS,
    }
    target = process or psutil.Process()
    try:
        target.nice(levels[wanted])
    except (psutil.Error, OSError):
        return None
    return wanted


def llm_options(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    options: dict[str, Any] = {}
    keep_alive = section.get("keep_alive")
    if keep_alive not in (None, ""):
        options["keep_alive"] = keep_alive
    for key in ("num_thread", "num_ctx", "num_gpu"):
        value = section.get(key)
        if value not in (None, ""):
            try:
                options[key] = int(value)
            except (TypeError, ValueError):
                continue
    return options


__all__ = ["PRIORITY_NAMES", "apply_process_priority", "llm_options"]
