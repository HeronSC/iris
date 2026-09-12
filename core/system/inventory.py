# File: core/system/inventory.py

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from core.system.probes import as_list, human_bytes, run_powershell

Runner = Callable[[str], Any]

HARDWARE_SCRIPT = (
    "$cs = Get-CimInstance Win32_ComputerSystem; $os = Get-CimInstance Win32_OperatingSystem; "
    "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1; $board = Get-CimInstance Win32_BaseBoard; "
    "$bios = Get-CimInstance Win32_BIOS; $gpus = Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, DriverVersion; "
    "$mem = Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity, Speed, Manufacturer; "
    "[pscustomobject]@{ Manufacturer=$cs.Manufacturer; Model=$cs.Model; TotalMemory=$cs.TotalPhysicalMemory; "
    "OS=$os.Caption; OSVersion=$os.Version; OSBuild=$os.BuildNumber; InstallDate=[string]$os.InstallDate; "
    "Cpu=$cpu.Name; Cores=$cpu.NumberOfCores; Threads=$cpu.NumberOfLogicalProcessors; MaxClock=$cpu.MaxClockSpeed; "
    "Board=($board.Manufacturer + ' ' + $board.Product); Bios=($bios.Manufacturer + ' ' + $bios.SMBIOSBIOSVersion); "
    "Gpus=@($gpus); Memory=@($mem) } | ConvertTo-Json -Compress -Depth 4"
)

SOFTWARE_SCRIPT = (
    "$keys = 'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*','HKLM:\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*','HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'; "
    "Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName } | "
    "Select-Object DisplayName, DisplayVersion, Publisher, InstallDate | Sort-Object DisplayName -Unique | ConvertTo-Json -Compress"
)

UPDATES_SCRIPT = (
    "$session = New-Object -ComObject Microsoft.Update.Session; $searcher = $session.CreateUpdateSearcher(); "
    "$result = $searcher.Search('IsInstalled=0 and IsHidden=0'); "
    "@($result.Updates | ForEach-Object { [pscustomobject]@{ Title=$_.Title; KB=(($_.KBArticleIDs | ForEach-Object { 'KB' + $_ }) -join ','); "
    "Severity=[string]$_.MsrcSeverity; Size=$_.MaxDownloadSize; Downloaded=$_.IsDownloaded } }) | ConvertTo-Json -Compress"
)

TEMPERATURE_SCRIPT = (
    "$zones = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue | "
    "Select-Object InstanceName, CurrentTemperature; $fans = Get-CimInstance Win32_Fan -ErrorAction SilentlyContinue | Select-Object Name, DesiredSpeed, ActiveCooling; "
    "[pscustomobject]@{ Zones=@($zones); Fans=@($fans) } | ConvertTo-Json -Compress -Depth 3"
)


def hardware(runner: Runner = run_powershell) -> dict[str, Any]:
    payload = runner(HARDWARE_SCRIPT)
    data = payload if isinstance(payload, dict) else {}
    gpus = [{"name": item.get("Name"), "vram": human_bytes(item.get("AdapterRAM")) if item.get("AdapterRAM") else "?", "driver": item.get("DriverVersion")} for item in as_list(data.get("Gpus"))]
    sticks = as_list(data.get("Memory"))
    return {
        "manufacturer": data.get("Manufacturer"),
        "model": data.get("Model"),
        "cpu": data.get("Cpu"),
        "cores": data.get("Cores"),
        "threads": data.get("Threads"),
        "max_clock_mhz": data.get("MaxClock"),
        "memory_total": human_bytes(data.get("TotalMemory")) if data.get("TotalMemory") else "?",
        "memory_sticks": [f"{human_bytes(item.get('Capacity'))} {item.get('Speed') or '?'} MHz {item.get('Manufacturer') or ''}".strip() for item in sticks],
        "board": data.get("Board"),
        "bios": data.get("Bios"),
        "gpus": gpus,
        "os": data.get("OS"),
        "os_version": f"{data.get('OSVersion')} build {data.get('OSBuild')}" if data.get("OSVersion") else None,
        "installed": str(data.get("InstallDate") or "")[:10],
    }


def installed_software(name_filter: str = "", *, limit: int = 60, runner: Runner = run_powershell) -> list[dict[str, Any]]:
    rows = as_list(runner(SOFTWARE_SCRIPT))
    needle = name_filter.strip().casefold()
    found = []
    for row in rows:
        name = str(row.get("DisplayName") or "")
        if needle and needle not in name.casefold() and needle not in str(row.get("Publisher") or "").casefold():
            continue
        found.append({"name": name, "version": str(row.get("DisplayVersion") or ""), "publisher": str(row.get("Publisher") or ""), "installed": str(row.get("InstallDate") or "")})
    return found[: max(1, limit)]


def pending_updates(*, runner: Runner = run_powershell) -> list[dict[str, Any]]:
    rows = as_list(runner(UPDATES_SCRIPT))
    return [{"title": str(row.get("Title") or ""), "kb": str(row.get("KB") or ""), "severity": str(row.get("Severity") or ""), "size": human_bytes(row.get("Size")) if row.get("Size") else "?", "downloaded": bool(row.get("Downloaded"))} for row in rows]


def temperatures(*, runner: Runner = run_powershell, gpu: Callable[[], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    payload = runner(TEMPERATURE_SCRIPT)
    data = payload if isinstance(payload, dict) else {}
    zones = []
    for item in as_list(data.get("Zones")):
        raw = item.get("CurrentTemperature")
        try:
            celsius = round(float(raw) / 10.0 - 273.15, 1)
        except (TypeError, ValueError):
            continue
        name = str(item.get("InstanceName") or "zone").split("\\")[-1]
        zones.append({"name": name, "celsius": celsius})
    fans = [{"name": str(item.get("Name") or "fan"), "speed": item.get("DesiredSpeed"), "active": bool(item.get("ActiveCooling"))} for item in as_list(data.get("Fans"))]
    gpus = []
    if gpu is not None:
        try:
            gpus = [{"name": item.get("name"), "celsius": item.get("temperature_c")} for item in gpu()]
        except Exception:
            gpus = []
    return {"zones": zones, "fans": fans, "gpus": gpus}


def largest_files(root: str | Path, *, top: int = 20, time_budget_seconds: float = 20.0) -> tuple[list[dict[str, Any]], bool]:
    base = Path(root)
    deadline = time.monotonic() + time_budget_seconds
    found: list[tuple[int, str, float]] = []
    truncated = False
    for current, _dirs, files in os.walk(base):
        if time.monotonic() > deadline:
            truncated = True
            break
        for name in files:
            path = Path(current) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((stat.st_size, str(path), stat.st_mtime))
    found.sort(reverse=True)
    return [{"path": path, "size": human_bytes(size), "bytes": size, "modified": time.strftime("%Y-%m-%d", time.localtime(mtime))} for size, path, mtime in found[: max(1, top)]], truncated


def recent_files(root: str | Path, *, hours: float = 24.0, top: int = 30, time_budget_seconds: float = 20.0) -> tuple[list[dict[str, Any]], bool]:
    base = Path(root)
    cutoff = time.time() - max(0.1, hours) * 3600.0
    deadline = time.monotonic() + time_budget_seconds
    found: list[tuple[float, str, int]] = []
    truncated = False
    for current, _dirs, files in os.walk(base):
        if time.monotonic() > deadline:
            truncated = True
            break
        for name in files:
            path = Path(current) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                found.append((stat.st_mtime, str(path), stat.st_size))
    found.sort(reverse=True)
    return [{"path": path, "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)), "size": human_bytes(size)} for mtime, path, size in found[: max(1, top)]], truncated


__all__ = ["hardware", "installed_software", "largest_files", "pending_updates", "recent_files", "temperatures"]
