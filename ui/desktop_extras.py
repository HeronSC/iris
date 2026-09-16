# File: ui/desktop_extras.py

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
VIRTUAL_KEYS = {"space": 0x20, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}
DEFAULT_HOTKEY = "ctrl+alt+i"
HOTKEY_ID = 0x4952
TALK_HOTKEY_ID = 0x4953
KEY_DOWN_MASK = 0x8000
ICON_PATH = Path(__file__).resolve().parent / "assets" / "iris.ico"
APP_MODEL_ID = "Iris.Assistant.Desktop"


def set_app_model_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_MODEL_ID)
    except Exception:
        logger.debug("Could not set the AppUserModelID", exc_info=True)


def make_icon(letter: str = "I", size: int = 64) -> QIcon:
    if ICON_PATH.exists():
        icon = QIcon(str(ICON_PATH))
        if not icon.isNull():
            return icon
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2563eb"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.setPen(QColor("#ffffff"))
    font = QFont("Segoe UI", int(size * 0.55), QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return QIcon(pixmap)


def parse_hotkey(text: str) -> tuple[int, int]:
    parts = [part.strip().lower() for part in (text or "").replace("-", "+").split("+") if part.strip()]
    if not parts:
        raise ValueError("Empty hotkey")
    modifiers = 0
    key: int | None = None
    for part in parts:
        if part in MODIFIERS:
            modifiers |= MODIFIERS[part]
        elif part in VIRTUAL_KEYS:
            key = VIRTUAL_KEYS[part]
        elif len(part) == 1 and (part.isalnum()):
            key = ord(part.upper())
        else:
            raise ValueError(f"Unknown key {part!r} in hotkey {text!r}")
    if key is None:
        raise ValueError(f"Hotkey {text!r} names no key")
    if modifiers == 0:
        raise ValueError(f"Hotkey {text!r} needs a modifier such as ctrl or alt")
    return modifiers, key


class GlobalHotkey(QAbstractNativeEventFilter):
    def __init__(
        self,
        on_press: Callable[[], None],
        *,
        hotkey_id: int = HOTKEY_ID,
        register: Callable[[int, int, int], bool] | None = None,
        unregister: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        self.on_press = on_press
        self.hotkey_id = int(hotkey_id)
        self._register = register or self._register_windows
        self._unregister = unregister or self._unregister_windows
        self.registered = False
        self.combination = DEFAULT_HOTKEY
        self.key = 0

    def register(self, combination: str = DEFAULT_HOTKEY) -> bool:
        try:
            modifiers, key = parse_hotkey(combination)
        except ValueError as error:
            logger.warning("Hotkey not registered: %s", error)
            return False
        try:
            self.registered = bool(self._register(self.hotkey_id, modifiers, key))
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            logger.warning("Hotkey %s could not be registered: %s", combination, error)
            self.registered = False
        self.combination = combination
        self.key = key
        if not self.registered:
            logger.warning("Hotkey %s is taken by another program", combination)
        return self.registered

    def unregister(self) -> None:
        if self.registered:
            try:
                self._unregister(self.hotkey_id)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.debug("Hotkey unregister failed: %s", error)
            self.registered = False

    def key_is_down(self) -> bool:
        if sys.platform != "win32" or not self.key:
            return False
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(self.key) & KEY_DOWN_MASK)
        except (OSError, AttributeError):
            return False

    def nativeEventFilter(self, event_type, message):
        if self.registered and self.is_hotkey_message(message):
            try:
                self.on_press()
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Hotkey handler failed: %s", error)
            return True, 0
        return False, 0

    def is_hotkey_message(self, message) -> bool:
        if sys.platform != "win32":
            return False
        try:
            address = int(message)
            payload = ctypes.wintypes.MSG.from_address(address)
        except (TypeError, ValueError, AttributeError):
            return False
        return int(payload.message) == WM_HOTKEY and int(payload.wParam) == self.hotkey_id

    @staticmethod
    def _register_windows(hotkey_id: int, modifiers: int, key: int) -> bool:
        if sys.platform != "win32":
            return False

        user32 = ctypes.windll.user32
        return bool(user32.RegisterHotKey(None, hotkey_id, modifiers | 0x4000, key))

    @staticmethod
    def _unregister_windows(hotkey_id: int) -> None:
        if sys.platform != "win32":
            return
        ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)


class TrayController(QObject):
    showRequested = Signal()
    hideRequested = Signal()
    quitRequested = Signal()
    muteToggled = Signal(bool)

    def __init__(self, parent: QWidget, *, title: str = "Iris", close_to_tray: bool = False, voice_muted: bool = False) -> None:
        super().__init__(parent)
        self.close_to_tray = close_to_tray
        self.icon = QSystemTrayIcon(make_icon(title[:1] or "I"), parent)
        self.icon.setToolTip(title)
        menu = QMenu(parent)
        self.show_action = QAction("Show Iris", menu)
        self.show_action.triggered.connect(self.showRequested)
        self.hide_action = QAction("Hide to tray", menu)
        self.hide_action.triggered.connect(self.hideRequested)
        self.close_action = QAction("Close to tray instead of quitting", menu)
        self.close_action.setCheckable(True)
        self.close_action.setChecked(close_to_tray)
        self.close_action.toggled.connect(self._set_close_to_tray)
        self.mute_action = QAction("Mute voice", menu)
        self.mute_action.setCheckable(True)
        self.mute_action.setChecked(voice_muted)
        self.mute_action.toggled.connect(self.muteToggled)
        self.quit_action = QAction("Quit Iris", menu)
        self.quit_action.triggered.connect(self.quitRequested)
        menu.addAction(self.show_action)
        menu.addAction(self.hide_action)
        menu.addSeparator()
        menu.addAction(self.close_action)
        menu.addAction(self.mute_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._activated)

    @property
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def start(self) -> None:
        if self.available:
            self.icon.show()

    def stop(self) -> None:
        self.icon.hide()

    def notify(self, title: str, body: str) -> None:
        if self.icon.isVisible():
            self.icon.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 4000)

    def _set_close_to_tray(self, checked: bool) -> None:
        self.close_to_tray = bool(checked)

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.showRequested.emit()


__all__ = ["DEFAULT_HOTKEY", "GlobalHotkey", "HOTKEY_ID", "TALK_HOTKEY_ID", "TrayController", "WM_HOTKEY", "apply_dark_palette", "apply_light_palette", "make_icon", "parse_hotkey"]


def apply_dark_palette(application: QApplication) -> QPalette:
    palette = QPalette()
    base = QColor(30, 30, 32)
    panel = QColor(40, 40, 44)
    text = QColor(228, 228, 230)
    accent = QColor(78, 140, 220)
    palette.setColor(QPalette.ColorRole.Window, base)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, panel)
    palette.setColor(QPalette.ColorRole.AlternateBase, base)
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, panel)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 120, 120))
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Link, QColor(120, 170, 240))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 140, 146))
    palette.setColor(QPalette.ColorRole.Mid, QColor(150, 150, 156))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(120, 120, 126))
    application.setStyle("Fusion")
    application.setPalette(palette)
    return palette


def apply_light_palette(application: QApplication) -> QPalette:
    palette = QPalette()
    window = QColor(243, 243, 245)
    base = QColor(255, 255, 255)
    text = QColor(24, 24, 27)
    accent = QColor(37, 99, 235)
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(246, 246, 248))
    palette.setColor(QPalette.ColorRole.ToolTipBase, base)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, QColor(238, 238, 241))
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(176, 32, 32))
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Link, QColor(29, 78, 216))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(130, 130, 138))
    palette.setColor(QPalette.ColorRole.Mid, QColor(110, 110, 118))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(160, 160, 166))
    application.setStyle("Fusion")
    application.setPalette(palette)
    return palette


class QuickInput(QDialog):
    submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, placeholder: str = "Ask Iris...") -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(False)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.setStyleSheet("QLineEdit { color: palette(text); background: palette(base); }")
        font = self.edit.font()
        font.setPointSize(max(12, font.pointSize() + 3))
        self.edit.setFont(font)
        self.edit.returnPressed.connect(self._submit)
        layout.addWidget(self.edit)
        self.hint = QLabel("Enter sends to Iris; Esc closes", self)
        self.hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self.hint)

    def open_centered(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.adjustSize()
            self.move(area.center().x() - self.width() // 2, area.top() + area.height() // 4)
        self.edit.clear()
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def _submit(self) -> None:
        text = self.edit.text().strip()
        if not text:
            return
        self.hide()
        self.submitted.emit(text)


class CommandPalette(QDialog):
    chosen = Signal(str)

    def __init__(self, commands: Sequence[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Commands")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(520, 360)
        self.commands = [(str(command), str(summary)) for command, summary in commands]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText("Type to filter commands")
        self.filter.textChanged.connect(self.refresh)
        self.filter.returnPressed.connect(self._choose_current)
        self.filter.installEventFilter(self)
        layout.addWidget(self.filter)
        self.list = QListWidget(self)
        self.list.itemActivated.connect(lambda _item: self._choose_current())
        layout.addWidget(self.list)
        self.refresh("")

    def refresh(self, needle: str) -> None:
        wanted = needle.strip().casefold()
        self.list.clear()
        for command, summary in self.commands:
            if wanted and wanted not in command.casefold() and wanted not in summary.casefold():
                continue
            item = QListWidgetItem(f"{command}    {summary}")
            item.setData(Qt.ItemDataRole.UserRole, command)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def visible_commands(self) -> list[str]:
        return [str(self.list.item(index).data(Qt.ItemDataRole.UserRole)) for index in range(self.list.count())]

    def open_at(self, anchor: QWidget | None = None) -> None:
        if anchor is not None:
            corner = anchor.mapToGlobal(anchor.rect().topLeft())
            self.move(corner.x(), max(0, corner.y() - self.height()))
        self.filter.clear()
        self.show()
        self.raise_()
        self.activateWindow()
        self.filter.setFocus()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.filter and event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            step = 1 if event.key() == Qt.Key.Key_Down else -1
            row = self.list.currentRow() + step
            if 0 <= row < self.list.count():
                self.list.setCurrentRow(row)
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def _choose_current(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        self.hide()
        self.chosen.emit(str(item.data(Qt.ItemDataRole.UserRole)))


PALETTE_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "List the commands"),
    ("/session list", "Recent sessions"),
    ("/session new", "Start a fresh session"),
    ("/session search ", "Find an earlier session by words"),
    ("/project", "Show the active project"),
    ("/project list", "All projects"),
    ("/knowledge browse", "Browse what Iris remembers"),
    ("/knowledge gaps", "Questions Iris could not answer"),
    ("/principles", "House rules Iris follows"),
    ("/corrections", "Corrections Iris has taken"),
    ("/tools", "Tools available right now"),
    ("/models", "Model routes and what is pulled"),
    ("/uncensored", "Pin replies to the unrestricted model"),
    ("/context", "What Iris sees on screen"),
    ("/search ", "Find files by words"),
    ("/index status", "Document index state"),
    ("/watch list", "Active watchers"),
    ("/schedule list", "Scheduled jobs"),
    ("/backup now", "Back up the data folder"),
    ("/changes", "Recent file edits, with undo"),
    ("/why", "Why the last answer came out that way"),
    ("/permissions", "Tool permission rules"),
    ("/eval", "Run the saved request checks"),
    ("/stop", "Stop the current work"),
)
