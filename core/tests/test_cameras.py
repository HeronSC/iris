# File: core/tests/test_cameras.py

"""Blue Iris (9.1), read-only: cameras, alerts with thumbnails, snapshots, and an offline watcher."""

from __future__ import annotations

import hashlib
import json
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from core.actions.implementations.camera_tools import CameraAlertsAction, CameraSnapshotAction, CameraStatusAction
from core.actions.models import ActionRequest
from core.cameras import BlueIrisError, CameraService
from core.cameras.blueiris import BlueIrisClient
from core.results.models import ResultKind
from core.watchers.checks import run_check
from core.watchers.models import WatcherContext

JPEG = b"\xff\xd8\xff\xe0fake"
NOW = int(time.time())


class FakeBlueIris:
    def __init__(self, *, offline: bool = False) -> None:
        self.session = "abc123"
        self.user = "henry"
        self.password = "secret"
        self.logins = 0
        self.calls: list[str] = []
        self.offline = offline

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/json":
            payload = json.loads(request.content.decode("utf-8"))
            cmd = payload.get("cmd")
            self.calls.append(cmd)
            if cmd == "login":
                if "response" not in payload:
                    return httpx.Response(200, json={"result": "fail", "session": self.session})
                expected = hashlib.md5(f"{self.user}:{self.session}:{self.password}".encode()).hexdigest()
                if payload.get("response") == expected:
                    self.logins += 1
                    return httpx.Response(200, json={"result": "success", "session": self.session, "data": {"system name": "NVR"}})
                return httpx.Response(200, json={"result": "fail"})
            if payload.get("session") != self.session:
                return httpx.Response(200, json={"result": "fail", "data": {"reason": "no session"}})
            if cmd == "camlist":
                return httpx.Response(
                    200,
                    json={
                        "result": "success",
                        "data": [
                            {"optionDisplay": "All cameras", "optionValue": "Index", "group": ["FD", "LR"]},
                            {"optionDisplay": "Front Door", "optionValue": "FD", "isOnline": True, "isRecording": True, "isMotion": False, "isEnabled": True, "FPS": 15, "width": 1920, "height": 1080, "nClips": 42, "nTriggers": 7},
                            {"optionDisplay": "Living Room", "optionValue": "LR", "isOnline": not self.offline, "isNoSignal": self.offline, "isRecording": False, "isEnabled": True, "FPS": 10, "width": 1280, "height": 720, "nClips": 3, "nTriggers": 1},
                        ],
                    },
                )
            if cmd == "alertlist":
                camera = payload.get("camera")
                alerts = [
                    {"path": "@1.dat", "camera": "FD", "date": NOW - 60, "memo": "person:91%", "zones": "A", "filesize": 1000, "res": "1920x1080"},
                    {"path": "@2.dat", "camera": "LR", "date": NOW - 3600 * 30, "memo": "", "zones": "B", "filesize": 900, "res": "1280x720"},
                    {"path": "@3.dat", "camera": "FD", "date": NOW - 7200, "memo": "car:80%", "zones": "A", "filesize": 800, "res": "1920x1080"},
                ]
                if camera and camera != "index":
                    alerts = [item for item in alerts if item["camera"] == camera]
                return httpx.Response(200, json={"result": "success", "data": alerts})
            if cmd == "status":
                return httpx.Response(200, json={"result": "success", "data": {"cpu": 12, "mem": "4 GB"}})
            if cmd == "logout":
                return httpx.Response(200, json={"result": "success"})
            return httpx.Response(200, json={"result": "fail"})
        if request.url.params.get("session") != self.session:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>login</html>")
        if request.url.path.startswith("/thumbs/") or request.url.path.startswith("/image/"):
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)
        return httpx.Response(404)


def _service(fake: FakeBlueIris) -> CameraService:
    client = BlueIrisClient("http://nvr.local:81", fake.user, fake.password, transport=httpx.MockTransport(fake.handle))
    return CameraService(client)


class ClientTests(unittest.TestCase):
    def test_login_is_a_challenge_response_and_the_session_is_reused(self) -> None:
        fake = FakeBlueIris()
        service = _service(fake)
        cameras = service.cameras()
        self.assertEqual([camera.short_name for camera in cameras], ["FD", "LR"])
        self.assertEqual(cameras[0].state, "online, recording")
        self.assertEqual(fake.logins, 1)
        service.cameras()
        self.assertEqual(fake.logins, 1)
        self.assertEqual(fake.calls.count("login"), 2)
        self.assertEqual(service.client.status()["cpu"], 12)

    def test_a_wrong_password_is_explained(self) -> None:
        fake = FakeBlueIris()
        client = BlueIrisClient("http://nvr.local:81", fake.user, "wrong", transport=httpx.MockTransport(fake.handle))
        with self.assertRaises(BlueIrisError) as caught:
            client.cameras()
        self.assertIn("refused the login", str(caught.exception))

    def test_alerts_are_filtered_by_time_and_camera_and_thumbnails_come_back(self) -> None:
        fake = FakeBlueIris()
        service = _service(fake)
        recent = service.alerts(hours=24, limit=10)
        self.assertEqual([alert.path for alert in recent], ["@1.dat", "@3.dat"])
        front = service.alerts("front door", hours=48, limit=10)
        self.assertEqual([alert.camera for alert in front], ["FD", "FD"])
        self.assertTrue(service.thumbnail(recent[0]).startswith("/9j/"))
        camera, payload = service.snapshot("LR")
        self.assertEqual(camera.name, "Living Room")
        self.assertTrue(payload.startswith("/9j/"))
        with self.assertRaises(BlueIrisError):
            service.alerts("garage")

    def test_configuration_comes_from_config_and_the_secret_store(self) -> None:
        unconfigured = CameraService.from_config({})
        self.assertFalse(unconfigured.configured)
        self.assertIn("blue_iris.url", unconfigured.problem)
        no_secret = CameraService.from_config({"blue_iris": {"url": "http://nvr.local:81", "user": "henry"}}, SimpleNamespace(get=lambda name: None))
        self.assertFalse(no_secret.configured)
        self.assertIn("/secrets set blue_iris:password", no_secret.problem)
        ready = CameraService.from_config({"blue_iris": {"url": "http://nvr.local:81", "user": "henry"}}, SimpleNamespace(get=lambda name: "secret"))
        self.assertTrue(ready.configured)
        self.assertEqual(ready.client.base_url, "http://nvr.local:81")


class ToolAndWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeBlueIris()
        self.service = _service(self.fake)
        self.execution = SimpleNamespace(cameras=self.service)

    def test_status_alerts_and_snapshot_tools(self) -> None:
        status_result = CameraStatusAction().execute(ActionRequest(action="camera_status", arguments={}), self.execution)
        self.assertEqual(status_result.status, "success")
        self.assertIn("2 cameras: 2 online, 1 recording", status_result.message)
        self.assertEqual([item.kind for item in status_result.results], [ResultKind.STATUS, ResultKind.TABLE])
        alerts = CameraAlertsAction()
        validation = alerts.validate(ActionRequest(action="camera_alerts", arguments={"camera": "Front Door", "hours": 48}), self.execution)
        self.assertTrue(validation.ok)
        result = alerts.execute(ActionRequest(action="camera_alerts", arguments=validation.resolved_arguments), self.execution)
        self.assertIn("2 alerts on Front Door in the last 48 hours", result.message)
        self.assertIn("person:91%", result.message)
        kinds = [item.kind for item in result.results]
        self.assertEqual(kinds, [ResultKind.TABLE, ResultKind.IMAGE, ResultKind.IMAGE])
        self.assertEqual(result.results[1].data["mime_type"], "image/jpeg")
        snapshot = CameraSnapshotAction().execute(ActionRequest(action="camera_snapshot", arguments={"camera": "front", "scale": 60}), self.execution)
        self.assertEqual(snapshot.results[0].kind, ResultKind.IMAGE)
        self.assertIn("Front Door right now", snapshot.message)
        missing = CameraSnapshotAction().execute(ActionRequest(action="camera_snapshot", arguments={"camera": "garage", "scale": 60}), self.execution)
        self.assertEqual(missing.status, "failed")
        unconfigured = CameraStatusAction().validate(ActionRequest(action="camera_status", arguments={}), SimpleNamespace(cameras=CameraService(None, configured=False, problem="not set up")))
        self.assertFalse(unconfigured.ok)
        self.assertEqual(unconfigured.error, "not set up")
        for tool in (CameraStatusAction(), CameraAlertsAction(), CameraSnapshotAction()):
            self.assertEqual(tool.definition.permission.value, "read")

    def test_the_offline_watcher_uses_the_camera_service(self) -> None:
        healthy = run_check("camera_offline", {}, None, WatcherContext(cameras=self.service))
        self.assertFalse(healthy.triggered)
        down = run_check("camera_offline", {}, None, WatcherContext(cameras=_service(FakeBlueIris(offline=True))))
        self.assertTrue(down.triggered)
        self.assertIn("Living Room", down.summary)
        unconfigured = run_check("camera_offline", {}, None, WatcherContext(cameras=CameraService(None, configured=False, problem="not set up")))
        self.assertFalse(unconfigured.triggered)
        self.assertIn("not set up", unconfigured.summary)


if __name__ == "__main__":
    unittest.main()
