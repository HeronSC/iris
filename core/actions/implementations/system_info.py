# File: core/actions/implementations/system_info.py

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.system import probes
from core.system.probes import human_bytes
from core.tools.models import PermissionLevel, ToolDefinition


class _ReadOnlyAction:

    name = ""
    definition: ToolDefinition
    arguments_model: type[BaseModel] | None = None

    def validate(self, request: ActionRequest, context: object) -> ValidationResult:
        if self.arguments_model is None:
            return ValidationResult(ok=True, resolved_arguments={})
        try:
            parsed = self.arguments_model.model_validate(request.arguments)
        except Exception as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        return ValidationResult(ok=True, resolved_arguments=parsed.model_dump())

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        try:
            message = self.render(request.arguments)
        except Exception as error:
            return ActionResult(status="failed", message=f"{self.name} failed: {error}", action=self.name, error=str(error))
        return ActionResult(status="success", message=message, action=self.name)

    def render(self, arguments: dict[str, Any]) -> str:
        raise NotImplementedError


class SystemOverviewAction(_ReadOnlyAction):
    name = "system_overview"
    definition = ToolDefinition(
        name="system_overview",
        description="How this PC is doing right now: CPU and RAM use, free space on each drive, GPU load and VRAM, uptime.",
        permission=PermissionLevel.READ,
        keywords=("system", "pc", "computer", "machine", "cpu", "ram", "memory", "gpu", "vram", "uptime", "slow", "performance", "resources"),
    )

    def render(self, arguments: dict[str, Any]) -> str:
        data = probes.overview()
        lines = [
            f"CPU {data['cpu_percent']:.0f}% of {data['cpu_count']} logical cores; "
            f"RAM {human_bytes(data['memory_used'])} of {human_bytes(data['memory_total'])} ({data['memory_percent']:.0f}%); "
            f"{data['process_count']} processes; up {data['uptime_hours']:.1f} h since {data['boot_time']}",
        ]
        for gpu in data.get("gpu", []):
            lines.append(
                f"GPU {gpu['name']}: {gpu['utilization_percent']:.0f}% busy, VRAM {gpu['vram_used_mb'] / 1024:.1f} of {gpu['vram_total_mb'] / 1024:.1f} GB, {gpu['temperature_c']:.0f} °C"
            )
        for disk in data.get("disks", []):
            lines.append(f"{disk['mount']} {human_bytes(disk['free'])} free of {human_bytes(disk['total'])} ({disk['percent']:.0f}% used)")
        return "\n".join(lines)


class DiskUsageArguments(BaseModel):
    path: str = Field(description="Drive or folder to measure, such as 'E:' or 'D:\\\\Videos'")
    top: int = Field(default=15, ge=1, le=50, description="How many of the largest entries to list")


class DiskUsageAction(_ReadOnlyAction):
    name = "disk_usage"
    arguments_model = DiskUsageArguments
    definition = ToolDefinition(
        name="disk_usage",
        description="What is taking space on a drive or in a folder: the largest folders and files directly inside it.",
        arguments=DiskUsageArguments,
        permission=PermissionLevel.READ,
        keywords=("space", "disk", "drive", "storage", "folder size", "big files", "largest", "full"),
    )

    def render(self, arguments: dict[str, Any]) -> str:
        data = probes.disk_usage(str(arguments.get("path", "")), top=int(arguments.get("top", 15)))
        lines = [f"Largest entries in {data['root']}:"]
        for entry in data["entries"]:
            marker = "" if entry["complete"] else " (partial)"
            lines.append(f"- {human_bytes(entry['size']):>9}  {entry['path']}{marker}")
        usage = data.get("usage")
        if usage:
            lines.append(f"Drive: {human_bytes(usage['used'])} used, {human_bytes(usage['free'])} free of {human_bytes(usage['total'])}")
        if data["truncated"]:
            lines.append("The scan hit its time budget; sizes marked partial are lower bounds.")
        if data["skipped"]:
            lines.append(f"{data['skipped']} entries could not be read.")
        return "\n".join(lines)


class TopProcessesArguments(BaseModel):
    sort_by: Literal["memory", "cpu"] = Field(default="memory", description="Rank by memory or by CPU")
    limit: int = Field(default=10, ge=1, le=50)


class TopProcessesAction(_ReadOnlyAction):
    name = "top_processes"
    arguments_model = TopProcessesArguments
    definition = ToolDefinition(
        name="top_processes",
        description="The processes using the most memory or CPU right now.",
        arguments=TopProcessesArguments,
        permission=PermissionLevel.READ,
        keywords=("process", "processes", "memory", "cpu", "hogging", "using the most", "task manager"),
    )

    def render(self, arguments: dict[str, Any]) -> str:
        sort_by = str(arguments.get("sort_by", "memory"))
        rows = probes.processes(sort_by=sort_by, limit=int(arguments.get("limit", 10)))
        lines = [f"Top processes by {sort_by}:"]
        for row in rows:
            lines.append(f"- {row['name']} (pid {row['pid']}): {human_bytes(row['memory'])} RAM, {row['cpu_percent']:.0f}% CPU")
        return "\n".join(lines)


class DriveHealthAction(_ReadOnlyAction):
    name = "drive_health"
    definition = ToolDefinition(
        name="drive_health",
        description="Health of each physical disk as Windows reports it, with SMART temperature, wear, and error counters when readable.",
        permission=PermissionLevel.READ,
        keywords=("drive", "disk", "ssd", "smart", "health", "failing", "wear"),
    )

    def render(self, arguments: dict[str, Any]) -> str:
        rows = probes.drive_health()
        if not rows:
            return "Windows reported no physical disks."
        lines = ["Drive health:"]
        counters_seen = False
        for row in rows:
            if row.get("device") == "smart":
                failing = [name for name, flag in row.get("predict_failure", {}).items() if flag]
                lines.append("SMART predicts failure on: " + (", ".join(failing) if failing else "none"))
                continue
            extras = []
            if row.get("temperature_c") is not None:
                extras.append(f"{row['temperature_c']} °C")
                counters_seen = True
            if row.get("wear_percent") is not None:
                extras.append(f"wear {row['wear_percent']}%")
            if row.get("power_on_hours") is not None:
                extras.append(f"{row['power_on_hours']} h on")
            if row.get("read_errors") or row.get("write_errors"):
                extras.append(f"errors r{row.get('read_errors') or 0}/w{row.get('write_errors') or 0}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"- {row['name']} [{row.get('media')}, {human_bytes(row.get('size'))}]: {row['health']}, {row['status']}{suffix}")
        if not counters_seen:
            lines.append("SMART counters need an elevated Iris to read; the health verdict above is Windows' own.")
        return "\n".join(lines)


class ServicesArguments(BaseModel):
    name_filter: str = Field(default="", description="Part of a service name to match, empty for all")
    running_only: bool = Field(default=False)
    limit: int = Field(default=40, ge=1, le=200)


class WindowsServicesAction(_ReadOnlyAction):
    name = "windows_services"
    arguments_model = ServicesArguments
    definition = ToolDefinition(
        name="windows_services",
        description="Windows services and whether each is running, optionally filtered by name.",
        arguments=ServicesArguments,
        permission=PermissionLevel.READ,
        keywords=("service", "services", "running", "daemon", "stopped"),
    )

    def render(self, arguments: dict[str, Any]) -> str:
        rows = probes.services(str(arguments.get("name_filter", "")), limit=int(arguments.get("limit", 40)), running_only=bool(arguments.get("running_only", False)))
        if not rows:
            return "No services matched."
        lines = [f"{len(rows)} services:"]
        for row in rows:
            lines.append(f"- {row['name']} ({row['display_name']}): {row['status']}, start {row['start_type']}")
        return "\n".join(lines)


class EventLogArguments(BaseModel):
    hours: float = Field(default=24.0, gt=0, le=24 * 30, description="How far back to look")
    limit: int = Field(default=20, ge=1, le=100)


class EventLogErrorsAction(_ReadOnlyAction):
    name = "event_log_errors"
    arguments_model = EventLogArguments
    definition = ToolDefinition(
        name="event_log_errors",
        description="Recent errors and critical events from the Windows System and Application event logs.",
        arguments=EventLogArguments,
        permission=PermissionLevel.READ,
        keywords=("event log", "errors", "crash", "crashed", "crashes", "faulting", "bluescreen", "bsod"),
    )

    def render(self, arguments: dict[str, Any]) -> str:
        hours = float(arguments.get("hours", 24.0))
        rows = probes.event_log_errors(hours, limit=int(arguments.get("limit", 20)))
        if not rows:
            return f"No errors in the System or Application logs in the last {hours:g} hours."
        lines = [f"{len(rows)} errors in the last {hours:g} hours:"]
        for row in rows:
            lines.append(f"- {row['time']} {row['log']}/{row['source']} #{row['id']}: {row['message'][:160]}")
        return "\n".join(lines)


class StartupAppsAction(_ReadOnlyAction):
    name = "startup_apps"
    definition = ToolDefinition(
        name="startup_apps",
        description="Programs that start with Windows, and where each is registered.",
        permission=PermissionLevel.READ,
        keywords=("startup", "boot", "starts with windows", "autostart", "login items"),
    )

    def render(self, arguments: dict[str, Any]) -> str:
        rows = probes.startup_items()
        if not rows:
            return "No startup items registered."
        lines = [f"{len(rows)} startup items:"]
        for row in rows:
            lines.append(f"- {row['name']}: {row['command']}  [{row['location']}, {row['user']}]")
        return "\n".join(lines)


SYSTEM_ACTIONS = (
    SystemOverviewAction,
    DiskUsageAction,
    TopProcessesAction,
    DriveHealthAction,
    WindowsServicesAction,
    EventLogErrorsAction,
    StartupAppsAction,
)
