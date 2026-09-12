# File: core/cameras/blueiris.py

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
PASSWORD_SECRET = "blue_iris:password"


class BlueIrisError(RuntimeError):
    pass


@dataclass(frozen=True)
class Camera:
    short_name: str
    name: str
    online: bool
    recording: bool
    motion: bool
    alerting: bool
    enabled: bool
    no_signal: bool
    fps: float
    width: int
    height: int
    clips: int
    triggers: int
    group: tuple[str, ...] = ()

    @property
    def is_group(self) -> bool:
        return bool(self.group) or self.short_name.startswith(("+", "@"))

    @property
    def state(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.online or self.no_signal:
            return "offline"
        parts = ["online"]
        if self.recording:
            parts.append("recording")
        if self.motion:
            parts.append("motion")
        if self.alerting:
            parts.append("alerting")
        return ", ".join(parts)


@dataclass(frozen=True)
class Alert:
    path: str
    camera: str
    at: datetime
    memo: str = ""
    zones: str = ""
    size_bytes: int = 0
    resolution: str = ""
    flags: int = 0

    @property
    def when(self) -> str:
        return self.at.astimezone().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Session:
    id: str
    logged_in_at: float = field(default_factory=time.monotonic)


def _bool(value: Any) -> bool:
    return bool(value) and str(value).lower() not in {"0", "false", "no"}


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_camera(item: dict[str, Any]) -> Camera:
    group = item.get("group")
    return Camera(
        short_name=str(item.get("optionValue") or ""),
        name=str(item.get("optionDisplay") or item.get("optionValue") or ""),
        online=_bool(item.get("isOnline", True)),
        recording=_bool(item.get("isRecording")),
        motion=_bool(item.get("isMotion")),
        alerting=_bool(item.get("isAlerting")),
        enabled=_bool(item.get("isEnabled", True)),
        no_signal=_bool(item.get("isNoSignal")),
        fps=float(item.get("FPS") or 0.0),
        width=_int(item.get("width")),
        height=_int(item.get("height")),
        clips=_int(item.get("nClips")),
        triggers=_int(item.get("nTriggers")),
        group=tuple(str(member) for member in group) if isinstance(group, list) else (),
    )


def parse_alert(item: dict[str, Any]) -> Alert:
    stamp = _int(item.get("date"))
    return Alert(
        path=str(item.get("path") or ""),
        camera=str(item.get("camera") or ""),
        at=datetime.fromtimestamp(stamp, tz=timezone.utc),
        memo=str(item.get("memo") or ""),
        zones=str(item.get("zones") or ""),
        size_bytes=_int(item.get("filesize")),
        resolution=str(item.get("res") or ""),
        flags=_int(item.get("flags")),
    )


class BlueIrisClient:
    def __init__(self, base_url: str, user: str, password: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self._session: Session | None = None
        self._lock = threading.Lock()

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout_seconds, transport=self.transport, headers={"User-Agent": "Iris/1.0"})

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.post(f"{self.base_url}/json", json=payload)
        except httpx.ConnectError as error:
            raise BlueIrisError(f"Blue Iris at {self.base_url} is not reachable: {error}") from error
        except httpx.HTTPError as error:
            raise BlueIrisError(f"Blue Iris did not answer: {error}") from error
        if response.status_code >= 400:
            raise BlueIrisError(f"Blue Iris answered {response.status_code}")
        try:
            data = response.json()
        except ValueError as error:
            raise BlueIrisError("Blue Iris returned something other than JSON; is the web server address right?") from error
        return data if isinstance(data, dict) else {}

    def login(self) -> Session:
        with self._lock:
            first = self._post({"cmd": "login"})
            session_id = str(first.get("session") or "")
            if not session_id:
                raise BlueIrisError("Blue Iris did not offer a session")
            digest = hashlib.md5(f"{self.user}:{session_id}:{self.password}".encode("utf-8")).hexdigest()
            second = self._post({"cmd": "login", "session": session_id, "response": digest})
            if str(second.get("result")) != "success":
                raise BlueIrisError("Blue Iris refused the login; check the user, the password in Credential Manager, and that the user may use the web server")
            self._session = Session(id=str(second.get("session") or session_id))
            return self._session

    def _command(self, cmd: str, **fields: Any) -> dict[str, Any]:
        session = self._session or self.login()
        payload = {"cmd": cmd, "session": session.id, **fields}
        data = self._post(payload)
        if str(data.get("result")) != "success":
            self._session = None
            session = self.login()
            payload["session"] = session.id
            data = self._post(payload)
            if str(data.get("result")) != "success":
                raise BlueIrisError(f"Blue Iris refused {cmd}: {data.get('data') or data.get('result')}")
        return data

    def status(self) -> dict[str, Any]:
        data = self._command("status").get("data")
        return dict(data) if isinstance(data, dict) else {}

    def cameras(self, *, include_groups: bool = False) -> list[Camera]:
        raw = self._command("camlist").get("data") or []
        found = [parse_camera(item) for item in raw if isinstance(item, dict) and item.get("optionValue")]
        return [camera for camera in found if include_groups or not camera.is_group]

    def alerts(self, camera: str | None = None, *, since: datetime | None = None, limit: int = 20) -> list[Alert]:
        fields: dict[str, Any] = {"camera": camera or "index"}
        if since is not None:
            fields["startdate"] = int(since.timestamp())
        raw = self._command("alertlist", **fields).get("data") or []
        alerts = [parse_alert(item) for item in raw if isinstance(item, dict) and item.get("path")]
        if since is not None:
            alerts = [alert for alert in alerts if alert.at >= since]
        alerts.sort(key=lambda alert: alert.at, reverse=True)
        return alerts[: max(1, int(limit))]

    def _get_bytes(self, path: str, params: dict[str, Any]) -> bytes:
        session = self._session or self.login()
        query = {**params, "session": session.id}
        try:
            with self._client() as client:
                response = client.get(f"{self.base_url}{path}", params=query)
        except httpx.HTTPError as error:
            raise BlueIrisError(f"Blue Iris did not answer: {error}") from error
        if response.status_code >= 400 or not response.content:
            raise BlueIrisError(f"Blue Iris returned no image for {path} ({response.status_code})")
        if b"<html" in response.content[:200].lower():
            raise BlueIrisError(f"Blue Iris returned a page instead of an image for {path}; the session may have expired")
        return response.content

    def alert_image(self, alert: Alert) -> bytes:
        return self._get_bytes(f"/thumbs/{alert.path}", {})

    def snapshot(self, short_name: str, *, quality: int = 60, scale: int = 60) -> bytes:
        return self._get_bytes(f"/image/{short_name}", {"q": int(quality), "s": int(scale)})

    def logout(self) -> None:
        if self._session is None:
            return
        try:
            self._post({"cmd": "logout", "session": self._session.id})
        except BlueIrisError:
            pass
        self._session = None


__all__ = ["Alert", "BlueIrisClient", "BlueIrisError", "Camera", "PASSWORD_SECRET", "Session", "parse_alert", "parse_camera"]
