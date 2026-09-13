# File: core/actions/implementations/nas_tools.py

from __future__ import annotations

from typing import Any

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.nas import NasService, SynologyError, SynologyPermissionError
from core.results.models import Result, Source, status, table
from core.system.probes import human_bytes
from core.tools.models import PermissionLevel, ToolDefinition

NO_SERVICE = "The NAS is not available in this host."
NEEDS_ADMIN = "DSM keeps temperature, drive health and load for administrators, so this account sees capacity only. Add the account to the administrators group on the NAS to see the rest."


def _ready(context: object) -> tuple[NasService | None, str | None]:
    service = getattr(context, "nas", None)
    if service is None:
        return None, NO_SERVICE
    if not service.configured:
        return service, service.problem
    return service, None


def _source(name: str, service: NasService) -> Source:
    return Source(name, "tool", service.client.base_url if service.client else "synology")


class NasStatusAction:

    name = "nas_status"
    definition = ToolDefinition(
        name="nas_status",
        description="How the Synology NAS is doing: model, DSM version, uptime, temperature, CPU and memory load, and how full each volume is.",
        permission=PermissionLevel.READ,
        keywords=("nas", "synology", "nas status", "is the nas", "nas health", "nas temperature", "disk space on the nas", "how full"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        _service, problem = _ready(context)
        return ValidationResult(ok=False, error=problem) if problem else ValidationResult(ok=True, resolved_arguments={})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service, problem = _ready(context)
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="nas_unavailable")
        try:
            info = service.system_info()
            volumes, disks = service.storage()
            load: Any = None
            try:
                load = service.utilization()
            except SynologyError:
                load = None
        except SynologyPermissionError:
            return self._capacity_only(service)
        except SynologyError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="synology")

        source = _source(self.name, service)
        lines = [info.summary]
        if load is not None:
            lines.append(f"CPU {load.cpu_percent:.0f}%, memory {load.memory_percent:.0f}%")
        sick_volumes = [volume for volume in volumes if not volume.healthy]
        sick_disks = [disk for disk in disks if not disk.healthy]
        full = [volume for volume in volumes if volume.percent_used >= 90.0]
        for volume in volumes:
            lines.append(f"{volume.name}: {human_bytes(volume.used_bytes)} of {human_bytes(volume.total_bytes)} used ({volume.percent_used:.0f}%), {human_bytes(volume.free_bytes)} free, {volume.status}")

        state = "ok"
        headline = info.summary
        if sick_volumes or sick_disks:
            state = "error"
            names = [volume.name for volume in sick_volumes] + [disk.name for disk in sick_disks]
            headline = f"{info.model}: {', '.join(names)} needs attention"
        elif info.temperature_warning:
            state = "warning"
            headline = f"{info.model} is running hot"
        elif full:
            state = "warning"
            headline = f"{info.model}: {', '.join(volume.name for volume in full)} is nearly full"

        rows = [
            (volume.name, volume.status, volume.filesystem, human_bytes(volume.total_bytes), human_bytes(volume.used_bytes), human_bytes(volume.free_bytes), f"{volume.percent_used:.0f}%")
            for volume in volumes
        ]
        results: list[Result] = [status(state, headline, source=source, details={"model": info.model, "firmware": info.firmware, "serial": info.serial})]
        if rows:
            results.append(table(("volume", "status", "filesystem", "size", "used", "free", "full"), rows, source=source, title="Volumes"))
        return ActionResult(status="success", message="\n".join(lines), action=self.name, results=tuple(results))

    def _capacity_only(self, service: NasService) -> ActionResult:
        source = _source(self.name, service)
        try:
            host = service.hostname()
            shares = service.shares()
        except SynologyError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="synology")
        if not shares:
            return ActionResult(status="failed", message=NEEDS_ADMIN, action=self.name, error="nas_needs_admin")
        biggest = max(shares, key=lambda share: share.total_bytes)
        headline = f"{host or 'The NAS'}: {human_bytes(biggest.free_bytes)} free of {human_bytes(biggest.total_bytes)}"
        lines = [headline, NEEDS_ADMIN]
        for share in shares:
            lines.append(f"{share.name} ({share.real_path or share.path}): {human_bytes(share.free_bytes)} free of {human_bytes(share.total_bytes)}{', read only' if share.read_only else ''}")
        rows = [
            (share.name, share.real_path or share.path, human_bytes(share.total_bytes), human_bytes(share.free_bytes), f"{share.percent_used:.0f}%", "yes" if share.read_only else "no")
            for share in shares
        ]
        state = "warning" if biggest.percent_used >= 90.0 else "ok"
        results: list[Result] = [
            status(state, headline, source=source, details={"host": host, "reading": "capacity only"}),
            table(("share", "path", "size", "free", "full", "read only"), rows, source=source, title="Shares"),
        ]
        return ActionResult(status="success", message="\n".join(lines), action=self.name, results=tuple(results))


class NasStorageAction:

    name = "nas_storage"
    definition = ToolDefinition(
        name="nas_storage",
        description="Every drive in the Synology NAS with its model, size, slot, temperature, SMART result and whether the volume it belongs to is healthy.",
        permission=PermissionLevel.READ,
        keywords=("nas drives", "nas disks", "smart", "drive health", "synology disks", "is a drive failing", "nas volumes", "raid"),
    )

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        _service, problem = _ready(context)
        return ValidationResult(ok=False, error=problem) if problem else ValidationResult(ok=True, resolved_arguments={})

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        service, problem = _ready(context)
        if problem:
            return ActionResult(status="failed", message=problem, action=self.name, error="nas_unavailable")
        try:
            volumes, disks = service.storage()
        except SynologyPermissionError:
            return ActionResult(status="failed", message=NEEDS_ADMIN, action=self.name, error="nas_needs_admin")
        except SynologyError as error:
            return ActionResult(status="failed", message=str(error), action=self.name, error="synology")

        source = _source(self.name, service)
        if not disks and not volumes:
            message = "DSM reports no volumes or drives."
            return ActionResult(status="success", message=message, action=self.name, results=(status("warning", message, source=source),))

        sick = [disk for disk in disks if not disk.healthy]
        hot = [disk for disk in disks if disk.temperature_c is not None and disk.temperature_c >= 50.0]
        headline = f"{len(disks)} drive{'s' if len(disks) != 1 else ''} across {len(volumes)} volume{'s' if len(volumes) != 1 else ''}"
        state = "ok"
        if sick:
            state = "error"
            headline = f"{', '.join(disk.name for disk in sick)} reports {', '.join(sorted({disk.smart_status or disk.status for disk in sick}))}"
        elif hot:
            state = "warning"
            headline = f"{headline}; {', '.join(disk.name for disk in hot)} running warm"

        lines = [headline]
        for disk in disks:
            temperature = f"{disk.temperature_c:.0f} °C" if disk.temperature_c is not None else "?"
            lines.append(f"{disk.name} ({disk.slot or disk.id}): {disk.model} {human_bytes(disk.size_bytes)}, {disk.status}, SMART {disk.smart_status or 'unknown'}, {temperature}")
        rows = [
            (disk.name, disk.slot or disk.id, disk.model, human_bytes(disk.size_bytes), disk.status, disk.smart_status or "unknown", f"{disk.temperature_c:.0f}" if disk.temperature_c is not None else "?")
            for disk in disks
        ]
        results: list[Result] = [status(state, headline, source=source)]
        if rows:
            results.append(table(("drive", "bay", "model", "size", "status", "smart", "°C"), rows, source=source, title="Drives"))
        return ActionResult(status="success", message="\n".join(lines), action=self.name, results=tuple(results))


NAS_ACTIONS = (NasStatusAction, NasStorageAction)

__all__ = ["NAS_ACTIONS", "NasStatusAction", "NasStorageAction"]
