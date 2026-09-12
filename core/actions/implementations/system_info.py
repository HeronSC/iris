# File: core/actions/implementations/system_info.py

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from core.actions.models import ActionRequest, ActionResult, ValidationResult
from core.system import inventory, probes
from core.system.probes import human_bytes
from core.results.models import Result, Source, chart, status, table
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
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ValidationResult(ok=False, error=f"Invalid arguments: {error}")
        return ValidationResult(ok=True, resolved_arguments=parsed.model_dump())

    def execute(self, request: ActionRequest, context: object) -> ActionResult:
        try:
            message, results = self.produce(request.arguments)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            return ActionResult(status="failed", message=f"{self.name} failed: {error}", action=self.name, error=str(error))
        return ActionResult(status="success", message=message, action=self.name, results=results)

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        return self.render(arguments), ()

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

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        data = probes.disk_usage(str(arguments.get("path", "")), top=int(arguments.get("top", 15)))
        return self._render(data), self._results(data)

    def render(self, arguments: dict[str, Any]) -> str:
        return self.produce(arguments)[0]

    @staticmethod
    def _render(data: dict[str, Any]) -> str:
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

    @staticmethod
    def _results(data: dict[str, Any]) -> tuple[Result, ...]:
        source = Source("disk_usage", "tool", str(data["root"]))
        found: list[Result] = [
            table(
                ("Path", "Size", "Bytes", "Complete"),
                [(entry["path"], human_bytes(entry["size"]), int(entry["size"]), bool(entry["complete"])) for entry in data["entries"]],
                source=source,
                title=f"Largest entries in {data['root']}",
            )
        ]
        usage = data.get("usage")
        if usage:
            found.append(
                chart(
                    "pie",
                    [{"name": "space", "values": [int(usage["used"]), int(usage["free"])]}],
                    labels=("used", "free"),
                    units="bytes",
                    source=source,
                    title=f"{human_bytes(usage['used'])} used, {human_bytes(usage['free'])} free of {human_bytes(usage['total'])}",
                )
            )
        if data["truncated"] or data["skipped"]:
            notes = []
            if data["truncated"]:
                notes.append("the scan hit its time budget; partial sizes are lower bounds")
            if data["skipped"]:
                notes.append(f"{data['skipped']} entries could not be read")
            found.append(status("warning", "; ".join(notes), source=source))
        return tuple(found)


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


class HardwareInfoAction(_ReadOnlyAction):
    name = "hardware_info"
    definition = ToolDefinition(
        name="hardware_info",
        description="What this PC is: maker and model, CPU with cores and clock, memory total and sticks, board and BIOS, graphics cards with VRAM and driver, Windows edition and build, install date.",
        permission=PermissionLevel.READ,
        cost="seconds",
        keywords=("hardware", "what cpu", "what gpu", "how much ram", "motherboard", "bios", "windows version", "which windows", "specs", "this pc"),
    )

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        data = inventory.hardware()
        lines = [
            f"{data.get('manufacturer') or '?'} {data.get('model') or ''}".strip(),
            f"CPU {data.get('cpu') or '?'}: {data.get('cores') or '?'} cores, {data.get('threads') or '?'} threads, {data.get('max_clock_mhz') or '?'} MHz",
            f"Memory {data.get('memory_total')}" + (": " + ", ".join(data.get("memory_sticks") or []) if data.get("memory_sticks") else ""),
            f"Board {data.get('board') or '?'}; BIOS {data.get('bios') or '?'}",
        ]
        for gpu in data.get("gpus") or []:
            lines.append(f"GPU {gpu.get('name')}: {gpu.get('vram')} VRAM, driver {gpu.get('driver')}")
        lines.append(f"{data.get('os') or 'Windows'} {data.get('os_version') or ''}, installed {data.get('installed') or '?'}")
        source = Source("hardware_info", "tool", "this PC")
        rows = [(key.replace("_", " "), ", ".join(map(str, value)) if isinstance(value, list) else str(value)) for key, value in data.items() if key != "gpus" and value]
        rows.extend((f"gpu {index + 1}", f"{gpu.get('name')} {gpu.get('vram')}") for index, gpu in enumerate(data.get("gpus") or []))
        return "\n".join(lines), (table(("item", "value"), rows, source=source, title="Hardware"),)


class SoftwareArguments(BaseModel):
    name: str = Field(default="", description="Only programs whose name or publisher contains this")
    limit: int = Field(default=60, ge=1, le=300)


class InstalledSoftwareAction(_ReadOnlyAction):
    name = "installed_software"
    definition = ToolDefinition(
        name="installed_software",
        description="Programs installed on this PC from the Windows uninstall registry, with version, publisher and install date; optionally filtered by name.",
        arguments=SoftwareArguments,
        permission=PermissionLevel.READ,
        cost="seconds",
        keywords=("installed", "is x installed", "which version of", "programs", "software list", "do i have", "installed programs"),
    )
    arguments_model = SoftwareArguments

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        rows = inventory.installed_software(str(arguments.get("name") or ""), limit=int(arguments.get("limit") or 60))
        source = Source("installed_software", "tool", "uninstall registry")
        needle = str(arguments.get("name") or "").strip()
        if not rows:
            message = f"No installed program matches '{needle}'." if needle else "No installed programs were found."
            return message, (status("ok" if needle else "warning", message, source=source),)
        lines = [f"{len(rows)} program{'s' if len(rows) != 1 else ''}" + (f" matching '{needle}'" if needle else "") + ":"]
        lines.extend(f"{row['name']} {row['version']}".strip() + (f" ({row['publisher']})" if row["publisher"] else "") for row in rows[:60])
        return "\n".join(lines), (table(("program", "version", "publisher", "installed"), [(row["name"], row["version"], row["publisher"], row["installed"]) for row in rows], source=source, title="Installed software"),)


class WindowsUpdatesAction(_ReadOnlyAction):
    name = "windows_updates"
    definition = ToolDefinition(
        name="windows_updates",
        description="Windows updates waiting to be installed, with KB number, severity and size, from the Windows Update agent. Can take half a minute.",
        permission=PermissionLevel.READ,
        timeout_seconds=240.0,
        cost="up to a minute; asks the Windows Update agent",
        keywords=("windows update", "pending updates", "updates waiting", "patch", "kb", "update status"),
    )

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        rows = inventory.pending_updates()
        source = Source("windows_updates", "tool", "windows update")
        if not rows:
            message = "No Windows updates are waiting."
            return message, (status("ok", message, source=source),)
        lines = [f"{len(rows)} update{'s' if len(rows) != 1 else ''} waiting:"] + [f"{row['title']} {row['kb']} {row['severity']} {row['size']}".strip() for row in rows]
        return "\n".join(lines), (status("warning", lines[0], source=source), table(("update", "kb", "severity", "size", "downloaded"), [(row["title"], row["kb"], row["severity"], row["size"], "yes" if row["downloaded"] else "no") for row in rows], source=source, title="Pending updates"))


class TemperaturesAction(_ReadOnlyAction):
    name = "temperatures"
    definition = ToolDefinition(
        name="temperatures",
        description="CPU or board thermal zones and fans as Windows exposes them, and GPU temperature from the NVIDIA driver. Many boards expose nothing to Windows; the answer says so.",
        permission=PermissionLevel.READ,
        cost="seconds",
        keywords=("temperature", "how hot", "fan", "thermal", "overheating", "cooling"),
    )

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        data = inventory.temperatures(gpu=probes.gpu)
        source = Source("temperatures", "tool", "wmi and nvidia-smi")
        rows: list[tuple[Any, ...]] = [(item["name"], f"{item['celsius']:.1f}", "thermal zone") for item in data["zones"]]
        rows.extend((str(item.get("name")), f"{item.get('celsius')}", "gpu") for item in data["gpus"] if item.get("celsius") is not None)
        rows.extend((item["name"], str(item.get("speed") or "?"), "fan") for item in data["fans"])
        if not rows:
            message = "This board exposes no temperature or fan readings to Windows; a vendor tool or LibreHardwareMonitor would be needed."
            return message, (status("warning", message, source=source),)
        lines = [f"{row[0]}: {row[1]}" + (" °C" if row[2] != "fan" else " rpm") for row in rows]
        if not data["zones"]:
            lines.append("No thermal zones are exposed by this board; only the GPU reports.")
        return "\n".join(lines), (table(("sensor", "reading", "kind"), rows, source=source, title="Temperatures"),)


class LargeFilesArguments(BaseModel):
    path: str = Field(description="Folder to look under")
    top: int = Field(default=20, ge=1, le=100)


class LargestFilesAction(_ReadOnlyAction):
    name = "largest_files"
    definition = ToolDefinition(
        name="largest_files",
        description="The largest files under a folder, with size and modified date, scanned for up to twenty seconds.",
        arguments=LargeFilesArguments,
        permission=PermissionLevel.READ,
        timeout_seconds=60.0,
        cost="up to twenty seconds of disk reading",
        keywords=("largest files", "biggest files", "what is taking space", "big files", "space hogs"),
    )
    arguments_model = LargeFilesArguments

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        folder = str(arguments["path"])
        rows, truncated = inventory.largest_files(folder, top=int(arguments.get("top") or 20))
        source = Source("largest_files", "tool", folder)
        if not rows:
            message = f"No files found under {folder}."
            return message, (status("warning", message, source=source),)
        lines = [f"Largest files under {folder}" + (" (scan cut short at twenty seconds)" if truncated else "") + ":"] + [f"{row['size']:>10}  {row['modified']}  {row['path']}" for row in rows]
        results: list[Result] = [table(("size", "modified", "path"), [(row["size"], row["modified"], row["path"]) for row in rows], source=source, title=f"Largest files under {folder}")]
        if truncated:
            results.append(status("warning", "The scan stopped after twenty seconds; the list may miss deeper folders.", source=source))
        return "\n".join(lines), tuple(results)


class RecentFilesArguments(BaseModel):
    path: str = Field(description="Folder to look under")
    hours: float = Field(default=24.0, gt=0, le=8760)
    top: int = Field(default=30, ge=1, le=200)


class RecentFilesAction(_ReadOnlyAction):
    name = "recent_files"
    definition = ToolDefinition(
        name="recent_files",
        description="Files changed under a folder in the last so many hours, newest first, scanned for up to twenty seconds.",
        arguments=RecentFilesArguments,
        permission=PermissionLevel.READ,
        timeout_seconds=60.0,
        cost="up to twenty seconds of disk reading",
        keywords=("recently changed", "modified today", "what changed in", "recent files", "files from today", "touched recently"),
    )
    arguments_model = RecentFilesArguments

    def produce(self, arguments: dict[str, Any]) -> tuple[str, tuple[Result, ...]]:
        folder = str(arguments["path"])
        hours = float(arguments.get("hours") or 24.0)
        rows, truncated = inventory.recent_files(folder, hours=hours, top=int(arguments.get("top") or 30))
        source = Source("recent_files", "tool", folder)
        if not rows:
            message = f"Nothing under {folder} changed in the last {hours:g} hours."
            return message, (status("ok", message, source=source),)
        lines = [f"{len(rows)} file{'s' if len(rows) != 1 else ''} changed under {folder} in the last {hours:g} hours" + (" (scan cut short)" if truncated else "") + ":"] + [f"{row['modified']}  {row['size']:>9}  {row['path']}" for row in rows]
        return "\n".join(lines), (table(("modified", "size", "path"), [(row["modified"], row["size"], row["path"]) for row in rows], source=source, title=f"Recent files under {folder}"),)


SYSTEM_ACTIONS = (
    HardwareInfoAction,
    InstalledSoftwareAction,
    WindowsUpdatesAction,
    TemperaturesAction,
    LargestFilesAction,
    RecentFilesAction,
    SystemOverviewAction,
    DiskUsageAction,
    TopProcessesAction,
    DriveHealthAction,
    WindowsServicesAction,
    EventLogErrorsAction,
    StartupAppsAction,
)
