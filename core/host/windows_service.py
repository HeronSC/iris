# File: core/host/windows_service.py

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SERVICE_NAME = "Iris"
SERVICE_DISPLAY_NAME = "Iris Assistant Host"
SERVICE_DESCRIPTION = "Runs Iris's watchers, scheduled jobs, and HTTP surface when no window is open."

CONFIG_ENVIRONMENT = "IRIS_CONFIG_PATH"

RECOVERY_ACTIONS = "restart/5000/restart/30000/restart/60000"


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENVIRONMENT, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "core" / "config.json"


def available() -> bool:
    try:
        #! @allow-local-import
        import win32serviceutil
    except ImportError:
        return False
    return win32serviceutil is not None


def service_class() -> Any:
    #! @allow-local-import
    import servicemanager
    #! @allow-local-import
    import win32event
    #! @allow-local-import
    import win32service
    #! @allow-local-import
    import win32serviceutil

    #! @allow-local-import
    from core.host.service import IrisHost
    #! @allow-local-import
    from core.observability import configure_logging, log_dir_for

    class IrisWindowsService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.host: IrisHost | None = None

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self) -> None:
            config_path = default_config_path()
            configure_logging(log_dir_for(None, config_path))
            servicemanager.LogInfoMsg(f"{SERVICE_DISPLAY_NAME} starting with {config_path}")
            try:
                self.host = IrisHost(config_path)
                self.host.start()
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                servicemanager.LogErrorMsg(f"{SERVICE_DISPLAY_NAME} could not start: {error}")
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)
                return
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            self.host.stop()
            servicemanager.LogInfoMsg(f"{SERVICE_DISPLAY_NAME} stopped")

    return IrisWindowsService


def configure_recovery() -> int:
    command = ["sc", "failure", SERVICE_NAME, "reset=", "86400", "actions=", RECOVERY_ACTIONS]
    return subprocess.run(command, check=False).returncode


def handle_command_line(argv: list[str]) -> int:
    #! @allow-local-import
    import win32serviceutil

    arguments = list(argv)
    if arguments and arguments[-1] == "install" and "--startup" not in arguments:
        arguments = arguments[:-1] + ["--startup", "auto", "install"]
    win32serviceutil.HandleCommandLine(service_class(), argv=[sys.argv[0], *arguments])
    if arguments and arguments[-1] == "install":
        configure_recovery()
    return 0


__all__ = [
    "CONFIG_ENVIRONMENT",
    "SERVICE_DESCRIPTION",
    "SERVICE_DISPLAY_NAME",
    "SERVICE_NAME",
    "available",
    "configure_recovery",
    "default_config_path",
    "handle_command_line",
    "service_class",
]
