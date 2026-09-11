# File: core/watchers/checks.py

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.system import probes
from core.system.probes import human_bytes


@dataclass(frozen=True)
class CheckResult:
    triggered: bool
    summary: str
    value: Any = None
    clear_when: Callable[[Any], bool] | None = None


@dataclass(frozen=True)
class CheckKind:
    name: str
    description: str
    params: dict[str, str]
    run: Callable[[dict[str, Any], Any], CheckResult]
    needs_baseline: bool = False


def _mount(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 1:
        text += ":"
    if len(text) == 2 and text[1] == ":":
        text += "\\"
    return text


def disk_free_below(params: dict[str, Any], _baseline: Any) -> CheckResult:
    #! @allow-local-import
    import psutil

    mount = _mount(params.get("mount") or params.get("drive") or "C:")
    limit_gb = float(params.get("gb", 20))
    usage = psutil.disk_usage(mount)
    free_gb = usage.free / 1024**3
    triggered = free_gb < limit_gb
    summary = f"{mount} has {free_gb:.1f} GB free ({usage.percent:.0f}% used); limit {limit_gb:g} GB"
    return CheckResult(triggered, summary, round(free_gb, 2), clear_when=lambda value: float(value) >= limit_gb * 1.1)


def memory_percent_above(params: dict[str, Any], _baseline: Any) -> CheckResult:
    #! @allow-local-import
    import psutil

    limit = float(params.get("percent", 90))
    memory = psutil.virtual_memory()
    return CheckResult(
        memory.percent > limit,
        f"RAM at {memory.percent:.0f}% ({human_bytes(memory.used)} of {human_bytes(memory.total)}); limit {limit:g}%",
        float(memory.percent),
        clear_when=lambda value: float(value) <= max(0.0, limit - 5.0),
    )


def cpu_percent_above(params: dict[str, Any], _baseline: Any) -> CheckResult:
    #! @allow-local-import
    import psutil

    limit = float(params.get("percent", 90))
    percent = psutil.cpu_percent(interval=1.0)
    return CheckResult(percent > limit, f"CPU at {percent:.0f}%; limit {limit:g}%", float(percent), clear_when=lambda value: float(value) <= max(0.0, limit - 10.0))


def vram_percent_above(params: dict[str, Any], _baseline: Any) -> CheckResult:
    limit = float(params.get("percent", 90))
    gpus = probes.gpu()
    if not gpus:
        return CheckResult(False, "No GPU reported by nvidia-smi", None)
    worst = max(gpus, key=lambda gpu: gpu["vram_used_mb"] / max(1.0, gpu["vram_total_mb"]))
    percent = 100.0 * worst["vram_used_mb"] / max(1.0, worst["vram_total_mb"])
    return CheckResult(
        percent > limit,
        f"{worst['name']} VRAM at {percent:.0f}% ({worst['vram_used_mb'] / 1024:.1f} of {worst['vram_total_mb'] / 1024:.1f} GB); limit {limit:g}%",
        round(percent, 1),
        clear_when=lambda value: float(value) <= max(0.0, limit - 5.0),
    )


def service_not_running(params: dict[str, Any], _baseline: Any) -> CheckResult:
    name = str(params.get("name") or "").strip()
    if not name:
        raise ValueError("service_not_running needs name=<service>")
    rows = [row for row in probes.services(name, limit=20) if str(row.get("name", "")).lower() == name.lower()]
    if not rows:
        return CheckResult(True, f"Service {name} is not installed", "missing")
    status = str(rows[0]["status"])
    return CheckResult(status != "Running", f"Service {name} is {status}", status)


def drive_unhealthy(_params: dict[str, Any], _baseline: Any) -> CheckResult:
    rows = [row for row in probes.drive_health() if row.get("device") != "smart"]
    bad = [row for row in rows if str(row.get("health") or "").lower() not in {"healthy", ""}]
    predicted = [row for row in probes.drive_health() if row.get("device") == "smart" and any(row.get("predict_failure", {}).values())]
    if bad or predicted:
        names = ", ".join(f"{row['name']}: {row['health']}" for row in bad) or "SMART predicts a failure"
        return CheckResult(True, f"Drive health problem: {names}", names)
    return CheckResult(False, f"All {len(rows)} drives report Healthy", "healthy")


def event_log_errors(params: dict[str, Any], baseline: Any) -> CheckResult:
    hours = float(params.get("hours", 1))
    minimum = int(params.get("min_count", 1))
    rows = probes.event_log_errors(hours, limit=50)
    newest = rows[0]["time"] if rows else None
    count = len(rows)
    if baseline and newest and newest <= str(baseline):
        return CheckResult(False, f"No new errors since {baseline}", newest)
    triggered = count >= minimum and newest is not None
    head = f"{rows[0]['source']} #{rows[0]['id']}: {rows[0]['message'][:120]}" if rows else ""
    return CheckResult(triggered, f"{count} error(s) in the last {hours:g} h" + (f"; newest {newest} {head}" if rows else ""), newest)


def _path_signature(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if path.is_dir():
        try:
            entries = sorted(entry.name for entry in os.scandir(path))
        except OSError:
            entries = []
        return {"mtime": stat.st_mtime, "entries": len(entries), "newest": max((path / name).stat().st_mtime for name in entries) if entries else stat.st_mtime}
    return {"mtime": stat.st_mtime, "size": stat.st_size}


def path_changed(params: dict[str, Any], baseline: Any) -> CheckResult:
    target = Path(str(params.get("path") or "")).expanduser()
    if not str(target):
        raise ValueError("path_changed needs path=<file or folder>")
    signature = _path_signature(target)
    if signature is None:
        return CheckResult(baseline is not None, f"{target} is missing", None)
    if baseline is None:
        return CheckResult(False, f"Watching {target}", signature)
    changed = signature != baseline
    return CheckResult(changed, f"{target} changed" if changed else f"{target} unchanged", signature)


def path_missing(params: dict[str, Any], _baseline: Any) -> CheckResult:
    target = Path(str(params.get("path") or "")).expanduser()
    exists = target.exists()
    return CheckResult(not exists, f"{target} {'exists' if exists else 'is missing'}", exists)


def host_unreachable(params: dict[str, Any], _baseline: Any) -> CheckResult:
    host = str(params.get("host") or "").strip()
    port = int(params.get("port", 443))
    timeout = float(params.get("timeout", 3.0))
    if not host:
        raise ValueError("host_unreachable needs host=<name or ip> [port=443]")
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return CheckResult(False, f"{host}:{port} is reachable", True)
    except OSError as error:
        return CheckResult(True, f"{host}:{port} is unreachable ({error})", False)


KINDS: dict[str, CheckKind] = {
    kind.name: kind
    for kind in (
        CheckKind("disk_free_below", "Free space on a drive drops under a limit", {"mount": "drive letter, e.g. E:", "gb": "limit in GB (default 20)"}, disk_free_below),
        CheckKind("memory_percent_above", "RAM use rises above a percentage", {"percent": "limit (default 90)"}, memory_percent_above),
        CheckKind("cpu_percent_above", "CPU use rises above a percentage", {"percent": "limit (default 90)"}, cpu_percent_above),
        CheckKind("vram_percent_above", "GPU memory use rises above a percentage", {"percent": "limit (default 90)"}, vram_percent_above),
        CheckKind("service_not_running", "A Windows service is stopped or missing", {"name": "service name"}, service_not_running),
        CheckKind("drive_unhealthy", "Windows reports a physical disk as not Healthy", {}, drive_unhealthy),
        CheckKind("event_log_errors", "New errors appear in the System or Application log", {"hours": "window (default 1)", "min_count": "at least this many (default 1)"}, event_log_errors, needs_baseline=True),
        CheckKind("path_changed", "A file or folder changes", {"path": "file or folder"}, path_changed, needs_baseline=True),
        CheckKind("path_missing", "A file or folder disappears", {"path": "file or folder"}, path_missing),
        CheckKind("host_unreachable", "A host stops answering on a TCP port", {"host": "name or ip", "port": "port (default 443)"}, host_unreachable),
    )
}


def run_check(kind: str, params: dict[str, Any], baseline: Any) -> CheckResult:
    definition = KINDS.get(kind)
    if definition is None:
        raise ValueError(f"Unknown watcher kind: {kind}. Known: {', '.join(sorted(KINDS))}")
    return definition.run(params, baseline)
