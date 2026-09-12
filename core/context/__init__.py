# File: core/context/__init__.py

from __future__ import annotations

from core.context.models import ActiveContext, WindowInfo
from core.context.providers import ContextProvider, default_providers
from core.context.service import ContextService
from core.context.win32 import excel_application, foreground_window


def build_context_service(config: dict | None = None) -> ContextService:
    section = (config or {}).get("context", {}) if isinstance((config or {}).get("context"), dict) else {}
    service = ContextService(
        default_providers(excel_application),
        foreground=foreground_window,
        poll_seconds=float(section.get("poll_seconds", 1.5)),
        history_size=int(section.get("history", 12)),
    )
    if not bool(section.get("enabled", True)):
        service.paused = True
    return service


__all__ = [
    "ActiveContext",
    "ContextProvider",
    "ContextService",
    "WindowInfo",
    "build_context_service",
    "default_providers",
    "excel_application",
    "foreground_window",
]
