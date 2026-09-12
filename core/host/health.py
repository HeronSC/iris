# File: core/host/health.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

HOST_DESKTOP = "desktop"
HOST_SERVICE = "service"
HOST_MCP = "mcp"


def _safe(call: Any, default: Any = None) -> Any:
    try:
        return call()
    except Exception:
        return default


def _database_rows(service: Any) -> tuple[dict[str, str], list[str]]:
    rows: dict[str, str] = {}
    broken: list[str] = []
    backups = getattr(service, "backups", None)
    databases = dict(getattr(backups, "databases", {}) or {})
    if not databases and getattr(service, "database", None) is not None:
        databases["knowledge"] = service.database
    for name, database in databases.items():
        report = _safe(database.verify)
        if report is None:
            rows[name] = "unknown"
            continue
        rows[name] = report.summary
        if not report.ok:
            broken.append(f"{name}.db failed quick_check and is read-only")
    return rows, broken


def _model_rows(service: Any) -> tuple[dict[str, Any], list[str]]:
    router = getattr(service, "model_router", None)
    if router is None:
        return {}, []
    status = _safe(router.status, {}) or {}
    available = list(status.get("available", []) or [])
    routes = list(status.get("routes", []) or [])
    warnings: list[str] = []
    if not available:
        warnings.append("Ollama did not answer; no model is available")
    else:
        warnings.extend(f"{row['model']} ({row['task']}) is not pulled" for row in routes if not row.get("pulled"))
    return {
        "configured": str(getattr(router, "model", "") or ""),
        "available": available,
        "routes": routes,
    }, warnings


def _index_rows(service: Any) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    knowledge = getattr(service, "knowledge", None)
    if knowledge is not None:
        rows["records"] = _safe(knowledge.records.count, 0)
    embeddings = getattr(service, "embedding_index", None)
    if embeddings is not None:
        rows["memory_vectors"] = _safe(embeddings.count, 0) if _safe(lambda: embeddings.available, False) else None
    documents = getattr(service, "document_catalog", None)
    if documents is not None:
        rows["documents"] = _safe(documents.count_documents, 0)
    document_vectors = getattr(service, "document_embeddings", None)
    if document_vectors is not None:
        rows["document_vectors"] = _safe(document_vectors.count, 0) if _safe(lambda: document_vectors.available, False) else None
    roots = getattr(service, "document_roots", None)
    if callable(roots):
        statuses = _safe(roots, []) or []
        rows["roots"] = {str(item.path): ("online" if item.online else f"offline: {item.reason}") for item in statuses}
    return rows


def _watcher_rows(service: Any) -> tuple[dict[str, Any], list[str]]:
    watchers = getattr(service, "watchers", None)
    if watchers is None:
        return {}, []
    rows = _safe(watchers.describe, []) or []
    failing = [row["label"] for row in rows if row.get("last_error")]
    return {
        "defined": len(rows),
        "running": bool(getattr(watchers, "running", False)),
        "active": [row["label"] for row in rows if row.get("active")],
        "failing": failing,
    }, [f"watcher {name} cannot run its check" for name in failing]


def _schedule_rows(service: Any) -> tuple[dict[str, Any], list[str]]:
    schedules = getattr(service, "schedules", None)
    if schedules is None:
        return {}, []
    rows = _safe(schedules.describe, []) or []
    failing = [row["label"] for row in rows if row.get("last_error")]
    return {
        "defined": len(rows),
        "running": bool(getattr(schedules, "running", False)),
        "last_runs": {row["label"]: row.get("last_run") for row in rows},
        "failing": failing,
    }, [f"scheduled job {name} failed last time" for name in failing]


def health_report(service: Any, *, host: str) -> dict[str, Any]:
    databases, broken = _database_rows(service)
    models, model_warnings = _model_rows(service)
    watchers, watcher_warnings = _watcher_rows(service)
    schedules, schedule_warnings = _schedule_rows(service)
    budget = getattr(service, "latency_budget", None)
    metrics = getattr(service, "request_metrics", None)
    if budget is not None and metrics is not None:
        model_warnings.extend(_safe(lambda: budget.check(metrics.summary(24.0)), []) or [])
    offline_roots = [path for path, state in (_index_rows(service).get("roots") or {}).items() if state != "online"]
    root_warnings = [f"document root {path} is offline" for path in offline_roots]
    problems = broken + model_warnings + watcher_warnings + schedule_warnings + root_warnings
    return {
        "status": "degraded" if problems else "ok",
        "host": host,
        "assistant": str((getattr(service, "config", {}) or {}).get("assistant_name", "Iris")),
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "models": models,
        "indexed": _index_rows(service),
        "databases": databases,
        "watchers": watchers,
        "schedules": schedules,
        "backups": _safe(lambda: getattr(service.backups.latest(), "name", None)) if getattr(service, "backups", None) else None,
        "broken": problems,
    }


__all__ = ["HOST_DESKTOP", "HOST_MCP", "HOST_SERVICE", "health_report"]
