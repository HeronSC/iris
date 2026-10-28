# File: ui/room_view.py

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QFile, QIODevice, QMimeDatabase, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtQuick import QQuickView
from PySide6.QtWebEngineCore import QWebEngineUrlRequestJob, QWebEngineUrlScheme, QWebEngineUrlSchemeHandler
from PySide6.QtWebEngineQuick import QQuickWebEngineProfile, QtWebEngineQuick
from PySide6.QtWidgets import QVBoxLayout, QWidget

#! @allow-local-import
from ui.room.manifest import build_manifest

SCHEME = b"iris-room"
ROOM_URL = "iris-room://room/index.html"
ROOM_DIR = Path(__file__).resolve().parent / "room"
HOST_QML = Path(__file__).resolve().parent / "room_host.qml"
BACKGROUND = "#15171a"
DEFAULT_ASSETS = "E:/Reallusion/Iris"
MIME_OVERRIDES = {".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json", ".glb": "model/gltf-binary", ".html": "text/html", ".css": "text/css"}
CONSOLE_LEVELS = {2: logging.ERROR, 1: logging.INFO}

logger = logging.getLogger("iris.room")


def register_room_scheme() -> None:
    os.environ.setdefault("QSG_RENDER_LOOP", "threaded")
    scheme = QWebEngineUrlScheme(SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.LocalAccessAllowed | QWebEngineUrlScheme.Flag.CorsEnabled | QWebEngineUrlScheme.Flag.FetchApiAllowed)
    QWebEngineUrlScheme.registerScheme(scheme)
    QtWebEngineQuick.initialize()


def room_settings(config_path: Path) -> dict | None:
    if not config_path.exists():
        return None
    config = json.loads(config_path.read_text(encoding="utf-8"))
    room = config.get("ui", {}).get("room")
    if not isinstance(room, dict):
        return None
    return {"assets": Path(str(room.get("assets") or DEFAULT_ASSETS)), "dev": bool(room.get("dev", False))}


class RoomSchemeHandler(QWebEngineUrlSchemeHandler):
    def __init__(self, room_dir: Path, assets_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.room_dir = room_dir.resolve()
        self.assets_dir = assets_dir.resolve()
        self.mime = QMimeDatabase()

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        path = urllib.parse.unquote(job.requestUrl().path()).lstrip("/") or "index.html"
        if path == "assets/manifest.json":
            self._reply_bytes(job, "application/json", json.dumps(build_manifest(self.assets_dir)).encode("utf-8"))
            return
        if path.startswith("assets/"):
            root, relative = self.assets_dir, path[len("assets/"):]
        else:
            root, relative = self.room_dir, path
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            logger.warning("Room page asked for %s, which is not there", path)
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        device = QFile(str(target), job)
        if not device.open(QIODevice.OpenModeFlag.ReadOnly):
            job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            return
        job.reply(QByteArray(self._content_type(target).encode("ascii")), device)

    def _reply_bytes(self, job: QWebEngineUrlRequestJob, content_type: str, body: bytes) -> None:
        buffer = QBuffer(job)
        buffer.setData(QByteArray(body))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(QByteArray(content_type.encode("ascii")), buffer)

    def _content_type(self, target: Path) -> str:
        override = MIME_OVERRIDES.get(target.suffix.lower())
        if override:
            return override
        return self.mime.mimeTypeForFile(str(target), QMimeDatabase.MatchMode.MatchExtension).name()


class RoomBridge(QObject):
    stateChanged = Signal("QVariantMap")
    speechLevel = Signal(float)
    commandRequested = Signal(str)
    viewChanged = Signal(str)
    monitorImage = Signal(str)

    ready = Signal()
    chatRequested = Signal()
    wakeToggled = Signal()
    progressed = Signal(float, float)
    failed = Signal(str)

    @Slot()
    def pageReady(self) -> None:
        logger.info("Room page is ready")
        self.ready.emit()

    @Slot()
    def openChat(self) -> None:
        self.chatRequested.emit()

    @Slot()
    def toggleWake(self) -> None:
        self.wakeToggled.emit()

    @Slot(float, float)
    def progress(self, loaded: float, total: float) -> None:
        self.progressed.emit(loaded, total)

    @Slot(str)
    def loadError(self, message: str) -> None:
        logger.error("Room page failed to load: %s", message)
        self.failed.emit(message)

    @Slot(str, bool)
    def log(self, message: str, is_error: bool) -> None:
        logger.log(logging.WARNING if is_error else logging.DEBUG, "room: %s", message)

    @Slot(int, str, int, str)
    def consoleMessage(self, level: int, message: str, line_number: int, source_id: str) -> None:
        logger.log(CONSOLE_LEVELS.get(level, logging.DEBUG), "room console: %s (%s:%s)", message, source_id.rsplit("/", 1)[-1], line_number)


class RoomView(QWidget):
    ready = Signal()
    chatRequested = Signal()
    wakeRequested = Signal()
    progressed = Signal(float, float)
    failed = Signal(str)

    def __init__(self, assets_dir: Path, parent: QWidget | None = None, *, room_dir: Path = ROOM_DIR) -> None:
        super().__init__(parent)
        profile = QQuickWebEngineProfile.defaultProfile()
        self.handler = RoomSchemeHandler(room_dir, assets_dir, self)
        if profile.urlSchemeHandler(SCHEME) is None:
            profile.installUrlSchemeHandler(SCHEME, self.handler)
        self.bridge = RoomBridge(self)
        self.bridge.ready.connect(self.ready)
        self.bridge.chatRequested.connect(self.chatRequested)
        self.bridge.wakeToggled.connect(self.wakeRequested)
        self.bridge.progressed.connect(self.progressed)
        self.bridge.failed.connect(self.failed)
        self.quick = QQuickView()
        self.quick.setColor(QColor(BACKGROUND))
        self.quick.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        self.quick.rootContext().setContextProperty("host", self.bridge)
        self.quick.setSource(QUrl.fromLocalFile(str(HOST_QML)))
        self.web = self.quick.rootObject()
        self.container = QWidget.createWindowContainer(self.quick, self)
        self.container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.container)
        self.setFocusProxy(self.container)
        if self.web is None:
            problems = "; ".join(error.toString() for error in self.quick.errors())
            logger.error("Room host failed to load %s: %s", HOST_QML.name, problems)
            QTimer.singleShot(0, lambda: self.failed.emit(f"host failed: {problems}"))

    def load_room(self, dev: bool = False) -> None:
        if self.web is not None:
            self.web.setProperty("url", QUrl(ROOM_URL + ("?dev" if dev else "")))

    def set_state(self, **changes: bool) -> None:
        self.bridge.stateChanged.emit(dict(changes))

    def speech_level(self, level: float) -> None:
        self.bridge.speechLevel.emit(float(level))

    def command(self, text: str) -> None:
        self.bridge.commandRequested.emit(text)

    def set_active(self, active: bool) -> None:
        self.bridge.viewChanged.emit("room" if active else "chat")

    def show_monitor_image(self, pixmap: QPixmap, width: int = 1024) -> None:
        if pixmap.isNull() or pixmap.width() < 100:
            return
        scaled = pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled.save(buffer, "JPG", 80)
        encoded = bytes(buffer.data().toBase64().data()).decode("ascii")
        self.bridge.monitorImage.emit(f"data:image/jpeg;base64,{encoded}")


__all__ = ["RoomView", "register_room_scheme", "room_settings"]
