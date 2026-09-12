# File: core/context/win32.py

from __future__ import annotations

import logging
from typing import Any

from core.context.models import WindowInfo

logger = logging.getLogger(__name__)


def foreground_window() -> WindowInfo | None:
    try:
        #! @allow-local-import
        import win32gui
        #! @allow-local-import
        import win32process
    except ImportError:
        return None
    try:
        handle = win32gui.GetForegroundWindow()
        if not handle:
            return None
        title = win32gui.GetWindowText(handle) or ""
        _thread_id, pid = win32process.GetWindowThreadProcessId(handle)
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        logger.debug("Foreground window lookup failed: %s", error)
        return None
    process_name, exe_path = _process_identity(int(pid))
    return WindowInfo(handle=int(handle), title=title, process_name=process_name, pid=int(pid), exe_path=exe_path)


def _process_identity(pid: int) -> tuple[str, str | None]:
    try:
        #! @allow-local-import
        import psutil

        process = psutil.Process(pid)
        name = process.name()
        try:
            exe_path = process.exe()
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
            exe_path = None
        return name, exe_path
    except (OSError, ValueError, RuntimeError, TypeError):
        return "", None


def excel_application() -> Any | None:
    try:
        #! @allow-local-import
        import pythoncom
        #! @allow-local-import
        import win32com.client
    except ImportError:
        return None
    try:
        pythoncom.CoInitialize()
        return win32com.client.GetActiveObject("Excel.Application")
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


__all__ = ["excel_application", "foreground_window"]
