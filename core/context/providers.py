# File: core/context/providers.py

from __future__ import annotations

import logging
import re
from pathlib import PurePath
from typing import Any, Callable, Protocol

from core.context.models import ActiveContext, WindowInfo

logger = logging.getLogger(__name__)

VSCODE_TITLE = re.compile(r"^(?P<dirty>[●•*] )?(?P<file>.+?) - (?P<project>.+?) - Visual Studio Code(?: - Insiders)?$")
VSCODE_PROJECT_ONLY = re.compile(r"^(?P<project>.+?) - Visual Studio Code(?: - Insiders)?$")
DOCUMENT_TITLE = re.compile(r"^(?P<dirty>\* ?)?(?P<name>.+?) - (?P<app>[^-]+)$")


class ContextProvider(Protocol):
    name: str

    def matches(self, window: WindowInfo) -> bool: ...

    def capture(self, window: WindowInfo) -> ActiveContext | None: ...


class VSCodeProvider:
    name = "vscode"
    processes = ("code.exe", "code - insiders.exe")

    def matches(self, window: WindowInfo) -> bool:
        return window.process_key in self.processes or window.title.endswith("Visual Studio Code")

    def capture(self, window: WindowInfo) -> ActiveContext | None:
        match = VSCODE_TITLE.match(window.title)
        if match:
            file_label = match.group("file").strip()
            project = match.group("project").strip()
            target = file_label if _looks_like_path(file_label) else None
            return ActiveContext(
                app="VS Code",
                title=window.title,
                provider=self.name,
                target=target,
                target_kind="file" if target else None,
                project=project,
                extra={"file": file_label, "unsaved": bool(match.group("dirty"))},
            )
        project_match = VSCODE_PROJECT_ONLY.match(window.title)
        if project_match:
            return ActiveContext(app="VS Code", title=window.title, provider=self.name, project=project_match.group("project").strip())
        return ActiveContext(app="VS Code", title=window.title, provider=self.name)


class ExcelProvider:
    name = "excel"

    def __init__(self, application: Callable[[], Any | None]) -> None:
        self.application = application

    def matches(self, window: WindowInfo) -> bool:
        return window.process_key == "excel.exe" or window.title.endswith(" - Excel")

    def capture(self, window: WindowInfo) -> ActiveContext | None:
        app = self.application()
        if app is None:
            return ActiveContext(app="Excel", title=window.title, provider=self.name, target=_title_document(window.title, "Excel"), target_kind="workbook")
        try:
            workbook = app.ActiveWorkbook
            path = str(workbook.FullName) if workbook is not None else None
            sheet = app.ActiveSheet.Name if getattr(app, "ActiveSheet", None) is not None else None
            selection = None
            selected = getattr(app, "Selection", None)
            if selected is not None:
                address = getattr(selected, "Address", None)
                selection = str(address(False, False)) if callable(address) else (str(address) if address else None)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.debug("Excel COM lookup failed: %s", error)
            return ActiveContext(app="Excel", title=window.title, provider=self.name, target=_title_document(window.title, "Excel"), target_kind="workbook")
        extra: dict[str, Any] = {}
        if sheet:
            extra["sheet"] = str(sheet)
        return ActiveContext(
            app="Excel",
            title=window.title,
            provider=self.name,
            target=path,
            target_kind="workbook" if path else None,
            selection=f"{sheet}!{selection}" if sheet and selection else selection,
            extra=extra,
        )


class ExplorerProvider:
    name = "explorer"

    def matches(self, window: WindowInfo) -> bool:
        return window.process_key == "explorer.exe" and bool(window.title.strip())

    def capture(self, window: WindowInfo) -> ActiveContext | None:
        title = window.title.strip()
        target = title if _looks_like_path(title) else None
        return ActiveContext(app="File Explorer", title=title, provider=self.name, target=target, target_kind="folder" if target else None, extra={"folder_name": title})


class OfficeDocumentProvider:
    name = "office"
    apps = {"winword.exe": "Word", "powerpnt.exe": "PowerPoint", "acrobat.exe": "Acrobat", "acrord32.exe": "Acrobat"}

    def matches(self, window: WindowInfo) -> bool:
        return window.process_key in self.apps

    def capture(self, window: WindowInfo) -> ActiveContext | None:
        app = self.apps[window.process_key]
        return ActiveContext(app=app, title=window.title, provider=self.name, target=_title_document(window.title, app), target_kind="document")


class WindowProvider:
    name = "window"

    def matches(self, window: WindowInfo) -> bool:
        return True

    def capture(self, window: WindowInfo) -> ActiveContext | None:
        app = _app_label(window)
        return ActiveContext(app=app, title=window.title, provider=self.name)


def _looks_like_path(value: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value)) or ("/" in value or "\\" in value) and "." in PurePath(value).name


def _title_document(title: str, app: str) -> str | None:
    match = DOCUMENT_TITLE.match(title.strip())
    if match and match.group("app").strip().lower().startswith(app.lower()):
        return match.group("name").strip()
    return None


def _app_label(window: WindowInfo) -> str:
    name = window.process_name or ""
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name or "Unknown application"


def default_providers(excel_application: Callable[[], Any | None]) -> tuple[ContextProvider, ...]:
    return (VSCodeProvider(), ExcelProvider(excel_application), OfficeDocumentProvider(), ExplorerProvider(), WindowProvider())


__all__ = [
    "ContextProvider",
    "ExcelProvider",
    "ExplorerProvider",
    "OfficeDocumentProvider",
    "VSCodeProvider",
    "WindowProvider",
    "default_providers",
]
