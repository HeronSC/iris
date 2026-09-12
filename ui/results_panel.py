# File: ui/results_panel.py

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette, QTextDocument
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QWidget

#! @allow-local-import
from core.results.html import CANCEL_URL, CONFIRM_URL, OPEN_SCHEME, render_approval_bar, render_page, render_results
#! @allow-local-import
from core.results.models import from_json_list


def markdown_to_html(body: str) -> str:
    document = QTextDocument()
    document.setMarkdown(body)
    rendered = document.toHtml()
    rendered = re.sub(r"^.*<body[^>]*>", "", rendered, flags=re.DOTALL)
    return re.sub(r"</body>.*$", "", rendered, flags=re.DOTALL)


def results_fragment(payload: Any) -> str:
    return render_results(from_json_list(payload), markdown=markdown_to_html)


def current_theme() -> str:
    palette = QApplication.palette()
    return "dark" if palette.color(QPalette.ColorRole.Window).lightness() < 128 else "light"


class ResultsPage(QWebEnginePage):
    commandRequested = Signal(str)
    openRequested = Signal(str)

    def acceptNavigationRequest(self, url: QUrl, navigation_type: QWebEnginePage.NavigationType, is_main_frame: bool) -> bool:
        text = url.toString()
        if url.scheme() == "iris":
            if text == CONFIRM_URL:
                self.commandRequested.emit("/confirm")
            elif text == CANCEL_URL:
                self.commandRequested.emit("/cancel")
            elif text.startswith(OPEN_SCHEME):
                target = parse_qs(urlsplit(text).query).get("path", [""])[0]
                if target:
                    self.openRequested.emit(target)
            return False
        if navigation_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False
        return True

    def javaScriptConsoleMessage(self, level: Any, message: str, line_number: int, source_id: str) -> None:
        return


class ResultsPanel(QWebEngineView):
    commandRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, base_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._page = ResultsPage(self)
        self._page.commandRequested.connect(self.commandRequested)
        self._page.openRequested.connect(self.open_path)
        self.setPage(self._page)
        settings = self._page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        self._base_url = QUrl.fromLocalFile(str((base_dir or Path.cwd()).resolve()) + "/")
        self.current_html = ""
        self.setMinimumWidth(280)
        self.show_sections([], title=None)

    def current_text(self) -> str:
        document = QTextDocument()
        document.setHtml(self.current_html)
        return document.toPlainText()

    def open_path(self, target: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def show_sections(self, sections: list[str], *, title: str | None, awaiting_approval: bool = False) -> None:
        blocks = [block for block in sections if block.strip()]
        body = '<hr class="section-break">'.join(f'<div class="section">{block}</div>' for block in blocks)
        if not body:
            body = '<p class="empty">No details available.</p>'
        if awaiting_approval:
            body = render_approval_bar() + body
        self.current_html = body
        self.setHtml(render_page(body, title=title, theme=current_theme()), self._base_url)

    def show_results(self, payload: Any, *, title: str | None) -> None:
        self.show_sections([results_fragment(payload)], title=title)


__all__ = ["ResultsPanel", "current_theme", "markdown_to_html", "results_fragment"]
