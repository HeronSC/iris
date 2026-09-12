# File: ui/desktop_extras.py

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import sys
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
VIRTUAL_KEYS = {"space": 0x20, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}
DEFAULT_HOTKEY = "ctrl+alt+i"
HOTKEY_ID = 0x4952


def make_icon(letter: str = "I", size: int = 64) -> QIcon:
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
    def __init__(self, on_press: Callable[[], None], *, register: Callable[[int, int, int], bool] | None = None, unregister: Callable[[int], None] | None = None) -> None:
        super().__init__()
        self.on_press = on_press
        self._register = register or self._register_windows
        self._unregister = unregister or self._unregister_windows
        self.registered = False
        self.combination = DEFAULT_HOTKEY

    def register(self, combination: str = DEFAULT_HOTKEY) -> bool:
        try:
            modifiers, key = parse_hotkey(combination)
        except ValueError as error:
            logger.warning("Hotkey not registered: %s", error)
            return False
        try:
            self.registered = bool(self._register(HOTKEY_ID, modifiers, key))
        except Exception as error:
            logger.warning("Hotkey %s could not be registered: %s", combination, error)
            self.registered = False
        self.combination = combination
        if not self.registered:
            logger.warning("Hotkey %s is taken by another program", combination)
        return self.registered

    def unregister(self) -> None:
        if self.registered:
            try:
                self._unregister(HOTKEY_ID)
            except Exception as error:
                logger.debug("Hotkey unregister failed: %s", error)
            self.registered = False

    def nativeEventFilter(self, event_type, message):
        if self.registered and self.is_hotkey_message(message):
            try:
                self.on_press()
            except Exception as error:
                logger.warning("Hotkey handler failed: %s", error)
            return True, 0
        return False, 0

    @staticmethod
    def is_hotkey_message(message) -> bool:
        if sys.platform != "win32":
            return False
        try:
            address = int(message)
            payload = ctypes.wintypes.MSG.from_address(address)
        except (TypeError, ValueError, AttributeError):
            return False
        return int(payload.message) == WM_HOTKEY and int(payload.wParam) == HOTKEY_ID

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

    def __init__(self, parent: QWidget, *, title: str = "Iris", close_to_tray: bool = False) -> None:
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
        self.quit_action = QAction("Quit Iris", menu)
        self.quit_action.triggered.connect(self.quitRequested)
        menu.addAction(self.show_action)
        menu.addAction(self.hide_action)
        menu.addSeparator()
        menu.addAction(self.close_action)
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


__all__ = ["DEFAULT_HOTKEY", "GlobalHotkey", "HOTKEY_ID", "TrayController", "WM_HOTKEY", "make_icon", "parse_hotkey"]
