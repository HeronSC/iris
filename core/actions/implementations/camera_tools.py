# File: core/actions/implementations/camera_tools.py

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.cameras import BlueIrisError, CameraService
from core.results.models import Result, Source, image, status, table
from core.tools.models import PermissionLevel, ToolDefinition

NO_SERVICE = "Cameras are not available in this host."


def _service(context: object) -> CameraService | None:
    return getattr(context, "cameras", None)


def _ready(context: object) -> tuple[CameraService | None, str | None]:
    service = _service(context)
    if service is None:
        return None, NO_SERVICE
    if not service.configured:
        return service, service.problem
    return service, None


class CameraStatusAction:

    name = "camera_status"
    definition = ToolDefinition(
        name="camera_status",
        description="Every camera in Blue Iris with whether it is online, recording, seeing motion or alerting, its frame rate and resolution, and how many clips and triggers it has.",
        permission=PermissionLevel.READ,
        keywords=("cameras", "camera status", "is the camera", "recording", "blue iris", "which cameras", "camera offline", "nvr"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        _service_obj, problem = _ready(context)
        return ValidationResult(ok=False, error=problem) if problem else ValidationResult(ok=True, resolved_arguments={})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service, problem = _ready(context)
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="cameras_unavailable")
        try:
            cameras = service.cameras()
        except BlueIrisError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="blue_iris")
        source = Source("camera_status", "tool", service.client.base_url if service.client else "blue iris")
        if not cameras:
            message = "Blue Iris reports no cameras."
            return ActionResult(status="success", message=message, action=self.name, results=(status("warning", message, source=source),))
        offline = [camera for camera in cameras if camera.enabled and (not camera.online or camera.no_signal)]
        recording = sum(1 for camera in cameras if camera.recording)
        lines = [f"{len(cameras)} camera{'s' if len(cameras) != 1 else ''}: {len(cameras) - len(offline)} online, {recording} recording" + (f", {len(offline)} offline ({', '.join(camera.name for camera in offline)})" if offline else "")]
        for camera in cameras:
            lines.append(f"{camera.name} ({camera.short_name}): {camera.state}, {camera.fps:.0f} fps, {camera.width}x{camera.height}, {camera.clips} clips, {camera.triggers} triggers")
        rows = [(camera.name, camera.short_name, camera.state, f"{camera.fps:.0f}", f"{camera.width}x{camera.height}", camera.clips, camera.triggers) for camera in cameras]
        results: list[Result] = [
            status("warning" if offline else "ok", lines[0], source=source),
            table(("camera", "id", "state", "fps", "size", "clips", "triggers"), rows, source=source, title="Cameras"),
        ]
        return ActionResult(status="success", message="\n".join(lines), action=self.name, results=tuple(results))


class AlertArguments(BaseModel):
    camera: str | None = Field(default=None, description="Camera name or id; every camera when omitted")
    hours: float = Field(default=24.0, gt=0, le=720, description="How far back to look")
    limit: int = Field(default=12, ge=1, le=60)
    thumbnails: bool = Field(default=True, description="Include a thumbnail of each alert")


class CameraAlertsAction:

    name = "camera_alerts"
    definition = ToolDefinition(
        name="camera_alerts",
        description="Recent motion and object alerts from Blue Iris, newest first, with time, camera, the zones or objects that triggered them, and a thumbnail of each.",
        arguments=AlertArguments,
        permission=PermissionLevel.READ,
        keywords=("alerts", "motion", "who was at", "anyone at the door", "what happened on the camera", "camera events", "last night on the camera", "detections"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = AlertArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        _service_obj, problem = _ready(context)
        if problem:
            return ValidationResult(ok=False, error=problem)
        return ValidationResult(ok=True, resolved_target=arguments.camera, resolved_arguments=arguments.model_dump())

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service, problem = _ready(context)
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="cameras_unavailable")
        arguments = request.arguments
        try:
            alerts = service.alerts(arguments.get("camera") or None, hours=float(arguments.get("hours") or 24.0), limit=int(arguments.get("limit") or 12))
        except BlueIrisError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="blue_iris")
        source = Source("camera_alerts", "tool", service.client.base_url if service.client else "blue iris")
        scope = f" on {arguments['camera']}" if arguments.get("camera") else ""
        if not alerts:
            message = f"No alerts{scope} in the last {float(arguments.get('hours') or 24):g} hours."
            return ActionResult(status="success", message=message, action=self.name, results=(status("ok", message, source=source),))
        lines = [f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''}{scope} in the last {float(arguments.get('hours') or 24):g} hours, newest first:"]
        results: list[Result] = []
        rows: list[tuple[Any, ...]] = []
        for index, alert in enumerate(alerts, start=1):
            detail = ", ".join(part for part in (alert.memo, alert.zones) if part)
            lines.append(f"{index}. {alert.when} {alert.camera}" + (f": {detail}" if detail else ""))
            rows.append((index, alert.when, alert.camera, detail, alert.path))
            if bool(arguments.get("thumbnails", True)):
                try:
                    results.append(image(mime_type="image/jpeg", base64=service.thumbnail(alert), source=Source("camera_alerts", "tool", alert.path), alt=f"{alert.camera} {alert.when}", title=f"{index}. {alert.camera} {alert.when}" + (f" — {detail}" if detail else "")))
                except BlueIrisError as error:
                    results.append(status("warning", f"No thumbnail for {alert.when} {alert.camera}: {error}", source=source))
        results.insert(0, table(("#", "when", "camera", "trigger", "clip"), rows, source=source, title="Alerts"))
        return ActionResult(status="success", message="\n".join(lines), action=self.name, resolved_target=arguments.get("camera"), results=tuple(results))


class SnapshotArguments(BaseModel):
    camera: str = Field(description="Camera name or id")
    scale: int = Field(default=60, ge=10, le=100, description="Percent of full size")


class CameraSnapshotAction:

    name = "camera_snapshot"
    definition = ToolDefinition(
        name="camera_snapshot",
        description="A current still image from one Blue Iris camera.",
        arguments=SnapshotArguments,
        permission=PermissionLevel.READ,
        keywords=("show me the camera", "snapshot", "what does the camera see", "look at the camera", "picture from the camera", "live view"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        try:
            arguments = SnapshotArguments.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        _service_obj, problem = _ready(context)
        if problem:
            return ValidationResult(ok=False, error=problem)
        return ValidationResult(ok=True, resolved_target=arguments.camera, resolved_arguments=arguments.model_dump())

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service, problem = _ready(context)
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="cameras_unavailable")
        try:
            camera, payload = service.snapshot(str(request.arguments.get("camera") or ""), scale=int(request.arguments.get("scale") or 60))
        except BlueIrisError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="blue_iris")
        source = Source("camera_snapshot", "tool", camera.short_name)
        message = f"{camera.name} right now ({camera.state})."
        return ActionResult(status="success", message=message, action=self.name, resolved_target=camera.short_name, results=(image(mime_type="image/jpeg", base64=payload, source=source, alt=camera.name, title=camera.name),))


CAMERA_ACTIONS = (CameraStatusAction, CameraAlertsAction, CameraSnapshotAction)

__all__ = ["CAMERA_ACTIONS", "CameraAlertsAction", "CameraSnapshotAction", "CameraStatusAction"]
