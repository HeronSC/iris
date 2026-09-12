# File: core/system/probes.py

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
_POWERSHELL_TIMEOUT = 60.0


def human_bytes(value: float | int | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in {"B", "KB"} else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def run_powershell(script: str, *, timeout: float = _POWERSHELL_TIMEOUT) -> Any:
    if not IS_WINDOWS:
        raise RuntimeError("PowerShell probes are Windows-only")
    wrapped = f"$ErrorActionPreference='SilentlyContinue'; $ProgressPreference='SilentlyContinue'; {script}"
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", wrapped],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    output = (completed.stdout or "").strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except ValueError:
        start = output.find("[") if output.find("[") >= 0 else output.find("{")
        if start < 0:
            raise RuntimeError(f"PowerShell returned no JSON: {output[:200]}")
        return json.loads(output[start:])


def as_list(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [payload]
    return [item for item in payload if isinstance(item, dict)]


_PS_DATE = re.compile(r"/Date\((-?\d+)\)/")


def powershell_date(value: Any) -> str:
    if isinstance(value, str):
        match = _PS_DATE.search(value)
        if match:
            return datetime.fromtimestamp(int(match.group(1)) / 1000).isoformat(timespec="minutes")
        return value
    return str(value or "")


def overview() -> dict[str, Any]:
    #! @allow-local-import
    import psutil

    memory = psutil.virtual_memory()
    boot = datetime.fromtimestamp(psutil.boot_time())
    uptime_hours = (datetime.now() - boot).total_seconds() / 3600
    disks: list[dict[str, Any]] = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except OSError:
            continue
        disks.append(
            {
                "mount": partition.mountpoint,
                "fstype": partition.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            }
        )
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "cpu_count": psutil.cpu_count(logical=True),
        "load_top": [],
        "memory_total": memory.total,
        "memory_used": memory.used,
        "memory_percent": memory.percent,
        "uptime_hours": round(uptime_hours, 1),
        "boot_time": boot.isoformat(timespec="minutes"),
        "process_count": len(psutil.pids()),
        "disks": disks,
        "gpu": gpu(),
    }


def gpu() -> list[dict[str, Any]]:
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return []
    try:
        completed = subprocess.run(
            [binary, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    gpus: list[dict[str, Any]] = []
    for line in (completed.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append(
                {
                    "name": parts[0],
                    "utilization_percent": float(parts[1]),
                    "vram_used_mb": float(parts[2]),
                    "vram_total_mb": float(parts[3]),
                    "temperature_c": float(parts[4]),
                }
            )
        except ValueError:
            continue
    return gpus


def processes(sort_by: str = "memory", limit: int = 10) -> list[dict[str, Any]]:
    #! @allow-local-import
    import psutil

    key = "memory" if sort_by not in {"cpu", "memory"} else sort_by
    rows: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "username"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            rows.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "?",
                    "memory": getattr(memory, "rss", 0) if memory else 0,
                    "cpu_percent": float(info.get("cpu_percent") or 0.0),
                    "user": info.get("username") or "",
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if key == "cpu":
        time.sleep(0.3)
        for row in rows:
            try:
                row["cpu_percent"] = psutil.Process(int(row["pid"])).cpu_percent(interval=None)
            except (psutil.Error, ValueError, TypeError):
                pass
    rows.sort(key=lambda row: row["cpu_percent"] if key == "cpu" else row["memory"], reverse=True)
    return rows[: max(1, limit)]


def disk_usage(root: str, *, top: int = 15, time_budget_seconds: float = 20.0) -> dict[str, Any]:
    base = Path(root).expanduser()
    if len(str(base).rstrip("\\/")) == 2 and str(base)[1] == ":":
        base = Path(str(base).rstrip("\\/") + "\\")
    if not base.exists():
        raise FileNotFoundError(f"{base} does not exist")
    deadline = time.monotonic() + time_budget_seconds
    entries: list[dict[str, Any]] = []
    truncated = False
    skipped = 0
    try:
        children = list(os.scandir(base))
    except OSError as error:
        raise PermissionError(f"Cannot list {base}: {error}") from error
    for child in children:
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            if child.is_symlink():
                skipped += 1
                continue
            if child.is_dir(follow_symlinks=False):
                size, complete = _tree_size(child.path, deadline)
                entries.append({"path": child.path, "size": size, "kind": "folder", "complete": complete})
                if not complete:
                    truncated = True
            else:
                entries.append({"path": child.path, "size": child.stat(follow_symlinks=False).st_size, "kind": "file", "complete": True})
        except OSError:
            skipped += 1
    entries.sort(key=lambda item: item["size"], reverse=True)
    total = sum(item["size"] for item in entries)
    usage: dict[str, Any] | None = None
    try:
        #! @allow-local-import
        import psutil

        stats = psutil.disk_usage(str(base))
        usage = {"total": stats.total, "used": stats.used, "free": stats.free, "percent": stats.percent}
    except (ImportError, OSError, ValueError, RuntimeError, TypeError):
        usage = None
    return {"root": str(base), "entries": entries[: max(1, top)], "measured": total, "usage": usage, "truncated": truncated, "skipped": skipped}


def _tree_size(path: str, deadline: float) -> tuple[int, bool]:
    total = 0
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return total, False
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, True


def drive_health() -> list[dict[str, Any]]:
    disks = as_list(run_powershell(
        "Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, BusType, HealthStatus, OperationalStatus, Size | ConvertTo-Json -Compress"
    ))
    counters = {}
    try:
        for row in as_list(run_powershell(
            "Get-PhysicalDisk | Get-StorageReliabilityCounter | Select-Object DeviceId, Temperature, Wear, ReadErrorsTotal, WriteErrorsTotal, PowerOnHours | ConvertTo-Json -Compress"
        )):
            counters[str(row.get("DeviceId"))] = row
    except (OSError, ValueError, RuntimeError, TypeError):
        counters = {}
    predictions = {}
    try:
        for row in as_list(run_powershell(
            "Get-CimInstance -Namespace root\\wmi -ClassName MSStorageDriver_FailurePredictStatus | Select-Object InstanceName, PredictFailure | ConvertTo-Json -Compress"
        )):
            predictions[str(row.get("InstanceName", ""))] = bool(row.get("PredictFailure"))
    except (OSError, ValueError, RuntimeError, TypeError):
        predictions = {}
    result: list[dict[str, Any]] = []
    for disk in disks:
        device = str(disk.get("DeviceId"))
        counter = counters.get(device, {})
        result.append(
            {
                "device": device,
                "name": disk.get("FriendlyName"),
                "media": disk.get("MediaType"),
                "bus": disk.get("BusType"),
                "health": disk.get("HealthStatus"),
                "status": disk.get("OperationalStatus"),
                "size": disk.get("Size"),
                "temperature_c": counter.get("Temperature"),
                "wear_percent": counter.get("Wear"),
                "read_errors": counter.get("ReadErrorsTotal"),
                "write_errors": counter.get("WriteErrorsTotal"),
                "power_on_hours": counter.get("PowerOnHours"),
            }
        )
    return result + ([{"device": "smart", "predict_failure": predictions}] if predictions else [])


_STATUS_NAMES = {1: "Stopped", 2: "StartPending", 3: "StopPending", 4: "Running", 5: "ContinuePending", 6: "PausePending", 7: "Paused"}
_START_TYPES = {0: "Boot", 1: "System", 2: "Automatic", 3: "Manual", 4: "Disabled"}


def services(name_filter: str = "", *, limit: int = 40, running_only: bool = False) -> list[dict[str, Any]]:
    pattern = name_filter.replace("'", "''")
    where = ""
    if pattern:
        where = f" | Where-Object {{ $_.Name -like '*{pattern}*' -or $_.DisplayName -like '*{pattern}*' }}"
    if running_only:
        where += " | Where-Object { $_.Status -eq 'Running' }"
    rows = as_list(run_powershell(
        f"Get-Service{where} | Select-Object -First {int(limit)} Name, DisplayName, Status, StartType | ConvertTo-Json -Compress"
    ))
    return [
        {
            "name": row.get("Name"),
            "display_name": row.get("DisplayName"),
            "status": _STATUS_NAMES.get(row.get("Status"), str(row.get("Status"))),
            "start_type": _START_TYPES.get(row.get("StartType"), str(row.get("StartType"))),
        }
        for row in rows
    ]


def event_log_errors(hours: float = 24.0, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = as_list(run_powershell(
        "Get-WinEvent -FilterHashtable @{LogName=@('System','Application'); Level=@(1,2); "
        f"StartTime=(Get-Date).AddHours(-{float(hours)})}} -MaxEvents {int(limit)} "
        "| Select-Object TimeCreated, LogName, ProviderName, Id, Message | ConvertTo-Json -Compress"
    ))
    return [
        {
            "time": powershell_date(row.get("TimeCreated")),
            "log": row.get("LogName"),
            "source": row.get("ProviderName"),
            "id": row.get("Id"),
            "message": " ".join(str(row.get("Message") or "").split())[:300],
        }
        for row in rows
    ]


def startup_items() -> list[dict[str, Any]]:
    rows = as_list(run_powershell("Get-CimInstance Win32_StartupCommand | Select-Object Name, Command, Location, User | ConvertTo-Json -Compress"))
    return [
        {"name": row.get("Name"), "command": row.get("Command"), "location": row.get("Location"), "user": row.get("User")}
        for row in rows
    ]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
