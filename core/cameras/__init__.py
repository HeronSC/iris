# File: core/cameras/__init__.py

from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.cameras.blueiris import PASSWORD_SECRET, Alert, BlueIrisClient, BlueIrisError, Camera

logger = logging.getLogger(__name__)

NOT_CONFIGURED = "Blue Iris is not configured: set blue_iris.url and blue_iris.user in config.json and the password with /secrets set blue_iris:password <value>."


class CameraService:
    def __init__(self, client: BlueIrisClient | None, *, configured: bool = True, problem: str | None = None) -> None:
        self.client = client
        self.configured = configured and client is not None
        self.problem = problem

    @classmethod
    def from_config(cls, config: dict[str, Any], secrets: Any = None, *, transport: Any = None) -> "CameraService":
        section = config.get("blue_iris") if isinstance(config.get("blue_iris"), dict) else {}
        url = str(section.get("url") or "").strip()
        user = str(section.get("user") or "").strip()
        if not url or not user:
            return cls(None, configured=False, problem=NOT_CONFIGURED)
        password = ""
        secret_name = str(section.get("password_secret") or PASSWORD_SECRET)
        if secrets is not None:
            try:
                password = secrets.get(secret_name) or ""
            except Exception as error:
                logger.warning("Could not read %s: %s", secret_name, error)
        if not password:
            return cls(None, configured=False, problem=f"Blue Iris password is not set: /secrets set {secret_name} <value>")
        return cls(BlueIrisClient(url, user, password, timeout_seconds=float(section.get("timeout_seconds", 10.0)), transport=transport))

    def _require(self) -> BlueIrisClient:
        if self.client is None:
            raise BlueIrisError(self.problem or NOT_CONFIGURED)
        return self.client

    def cameras(self) -> list[Camera]:
        return self._require().cameras()

    def find_camera(self, reference: str) -> Camera | None:
        wanted = reference.strip().casefold()
        if not wanted:
            return None
        cameras = self.cameras()
        for camera in cameras:
            if camera.short_name.casefold() == wanted or camera.name.casefold() == wanted:
                return camera
        partial = [camera for camera in cameras if wanted in camera.name.casefold() or wanted in camera.short_name.casefold()]
        return partial[0] if len(partial) == 1 else None

    def alerts(self, camera: str | None = None, *, hours: float = 24.0, limit: int = 12) -> list[Alert]:
        since = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(hours)))
        short_name = None
        if camera:
            found = self.find_camera(camera)
            if found is None:
                raise BlueIrisError(f"No camera called {camera}; /cameras lists them")
            short_name = found.short_name
        return self._require().alerts(short_name, since=since, limit=limit)

    def thumbnail(self, alert: Alert) -> str:
        return base64.b64encode(self._require().alert_image(alert)).decode("ascii")

    def snapshot(self, camera: str, *, quality: int = 60, scale: int = 60) -> tuple[Camera, str]:
        found = self.find_camera(camera)
        if found is None:
            raise BlueIrisError(f"No camera called {camera}")
        return found, base64.b64encode(self._require().snapshot(found.short_name, quality=quality, scale=scale)).decode("ascii")

    def offline(self) -> list[Camera]:
        return [camera for camera in self.cameras() if camera.enabled and (not camera.online or camera.no_signal)]


__all__ = ["Alert", "BlueIrisClient", "BlueIrisError", "Camera", "CameraService", "NOT_CONFIGURED", "PASSWORD_SECRET"]
