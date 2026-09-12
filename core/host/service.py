# File: core/host/service.py

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import structlog

from core.actions.bootstrap import build_action_layer
from core.actions.changes import ChangeLedger
from core.conversation.persistent_memory.models import MemoryConfig
from core.documents.catalog import DocumentCatalog
from core.host.health import HOST_SERVICE, health_report
from core.host.knowledge import IrisKnowledgeService
from core.scheduler.defaults import ensure_default_jobs
from core.scheduler.jobs import build_jobs
from core.scheduler.service import ScheduleService
from core.server.app import DEFAULT_HOST, DEFAULT_PORT, create_app
from core.storage.backups import DEFAULT_KEEP, BackupService
from core.storage.sqlite_database import SQLiteDatabase
from core.watchers.checks import register_kinds
from core.watchers.knowledge_checks import KNOWLEDGE_KINDS
from core.watchers.models import Notification, WatcherContext
from core.watchers.notify import InboxNotifier, LogNotifier, QuietHours
from core.watchers.service import WatcherService
from core.workflows.invokers import executor_confirmer, executor_invoker, executor_previewer
from core.workflows.runner import WorkflowRunner
from core.workflows.service import WorkflowService

logger = structlog.get_logger(__name__)

HEARTBEAT_SECONDS = 15.0

PROBE_TIMEOUT_SECONDS = 0.75


def probe_host(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class IrisHost:
    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        http_host: str = DEFAULT_HOST,
        http_port: int = DEFAULT_PORT,
        serve_http: bool = True,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.foundation = IrisKnowledgeService(config_path)
        self.config = self.foundation.config
        self.config_path = self.foundation.config_path
        self.knowledge = self.foundation.knowledge
        self.knowledge_retriever = self.foundation.knowledge_retriever
        self.knowledge_review = self.foundation.knowledge_review
        self.hypotheses = self.foundation.hypotheses
        self.embedding_index = self.foundation.embedding_index
        self.model_router = self.foundation.model_router
        self.audit_stream = self.foundation.audit_stream
        self.permissions = self.foundation.permissions
        self.secrets = self.foundation.secrets
        self.database = self.foundation.database
        self.data_root = self.foundation.data_root
        self.http_host = http_host
        self.http_port = http_port
        self.serve_http = serve_http
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._server: Any = None
        self.started = False

        document_cfg = self.config.get("document_search", {}) if isinstance(self.config.get("document_search"), dict) else {}
        catalog_path = document_cfg.get("catalog_path") or self.data_root / "Index" / "documents.db"
        self.document_database = SQLiteDatabase(catalog_path)
        self.document_catalog = DocumentCatalog(self.document_database)
        memory_config = MemoryConfig.from_config(self.config)
        metrics_path = self.config.get("metrics_path") or self.data_root / "Metrics" / "metrics.db"
        backups_cfg = self.config.get("backups") if isinstance(self.config.get("backups"), dict) else {}
        self.backups = BackupService(
            self.data_root / "Backups",
            databases={
                "knowledge": self.database,
                "documents": self.document_database,
                "metrics": SQLiteDatabase(metrics_path),
                "conversations": SQLiteDatabase(memory_config.database_path),
            },
            folders={
                "Memory": Path(self.config["memory_path"]),
                "Sessions": Path(self.config.get("session_path") or self.data_root / "Sessions"),
                "Configuration": self.data_root / "Configuration",
                "Audit": self.audit_stream.folder,
            },
            keep=int((backups_cfg or {}).get("keep", DEFAULT_KEEP)),
        )

        configuration = self.data_root / "Configuration"
        notifications_cfg = self.config.get("notifications", {}) if isinstance(self.config.get("notifications"), dict) else {}
        try:
            quiet = QuietHours.parse(str(notifications_cfg.get("quiet_hours") or ""))
        except ValueError as error:
            logger.warning("Ignoring notifications.quiet_hours", error=str(error))
            quiet = QuietHours()
        inbox = InboxNotifier(self.audit_stream.folder / "notifications.jsonl")
        notifiers: dict[str, Any] = {"log": LogNotifier(), "inbox": inbox}
        register_kinds(KNOWLEDGE_KINDS)
        self.watchers = WatcherService(
            configuration / "watchers.json",
            configuration / "watchers_state.json",
            notifiers,
            quiet_hours=quiet,
            inbox=inbox,
            context=WatcherContext(knowledge=self.knowledge),
        )
        self.schedules = ScheduleService(
            configuration / "schedules.json",
            configuration / "schedules_state.json",
            build_jobs(review=self.knowledge_review, backups=self.backups),
            notifiers=notifiers,
            quiet_hours=quiet,
            audit=self.audit_stream,
        )
        self.changes = ChangeLedger(self.data_root / "Backups" / "undo", audit=self.audit_stream)
        action_layer = build_action_layer(
            self.config,
            catalog=self.document_catalog,
            audit_folder=self.audit_stream.folder,
            permissions=self.permissions,
            ledger=self.changes,
            config_path=self.config_path,
            memory_path=Path(self.config["memory_path"]),
        )
        self.tool_registry = action_layer.tool_registry
        self.action_executor = action_layer.executor
        self.workflows = WorkflowService(
            self.data_root / "Workflows",
            WorkflowRunner(
                executor_invoker(self.action_executor),
                preview=executor_previewer(self.action_executor),
                confirm=executor_confirmer(self.action_executor),
                tool_version=self._tool_version,
            ),
            tool_version=self._tool_version,
            audit=self.audit_stream,
            notify=self._notify_workflow,
        )
        self.schedules.jobs.update(build_jobs(workflows=self.workflows))
        ensure_default_jobs(self.schedules)
        self.workflows.sync_schedules(self.schedules)
        self.watchers.listeners.append(self.workflows.watcher_listener())
        self.api = create_app(self) if serve_http else None

    def _tool_version(self, name: str) -> str | None:
        definition = self.tool_registry.get(name)
        return definition.version if definition is not None else None

    def _notify_workflow(self, run: Any) -> None:
        self.watchers.inbox.send(
            Notification(
                watcher_id=run.workflow_id,
                title=f"Workflow {run.workflow_name}: {run.status}",
                body=run.note or run.summary,
                kind="workflow",
                created_at=run.finished_at or run.started_at,
            )
        )

    def health(self) -> dict[str, Any]:
        report = health_report(self, host=HOST_SERVICE)
        report["http"] = {"host": self.http_host, "port": self.http_port, "running": self.http_running}
        report["halted"] = self.permissions.halted
        return report

    def halt(self) -> list[str]:
        halted: list[str] = []
        if not self.watchers.paused:
            self.watchers.pause()
            halted.append("watchers")
        if not self.schedules.paused:
            self.schedules.pause()
            halted.append("scheduled jobs")
        if not self.permissions.halted:
            self.permissions.halt("Iris is stopped")
            halted.append("tools")
        if not self.workflows.halted:
            self.workflows.halted = True
            halted.append("workflows")
        logger.warning("iris host halted", halted=halted)
        return halted

    def release(self) -> list[str]:
        released: list[str] = []
        if self.watchers.paused:
            self.watchers.resume()
            released.append("watchers")
        if self.schedules.paused:
            self.schedules.resume()
            released.append("scheduled jobs")
        if self.permissions.halted:
            self.permissions.release()
            released.append("tools")
        if self.workflows.halted:
            self.workflows.halted = False
            released.append("workflows")
        logger.info("iris host released", released=released)
        return released

    @property
    def http_running(self) -> bool:
        return self._server is not None and getattr(self._server, "started", False) and not self._server.should_exit

    def start(self) -> None:
        if self.started:
            return
        self._stop.clear()
        self.watchers.start()
        self.schedules.start()
        if self.api is not None:
            self._start_http()
        heartbeat = threading.Thread(target=self._heartbeat, name="iris-host-heartbeat", daemon=True)
        heartbeat.start()
        self._threads.append(heartbeat)
        self.started = True
        logger.info("iris host started", watchers=len(self.watchers.definitions()), schedules=len(self.schedules.definitions()), http=self.serve_http)

    def _start_http(self) -> None:
        #! @allow-local-import
        import uvicorn

        config = uvicorn.Config(self.api, host=self.http_host, port=self.http_port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        thread = threading.Thread(target=self._server.run, name="iris-host-http", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            for name, service in (("watchers", self.watchers), ("schedules", self.schedules), ("workflows", self.workflows)):
                try:
                    if service.reload_if_changed():
                        logger.info("reloaded definitions", service=name)
                        if name == "workflows":
                            self.workflows.sync_schedules(self.schedules)
                except Exception as error:
                    logger.warning("reload failed", service=name, error=str(error))

    def stop(self) -> None:
        if not self.started:
            return
        self._stop.set()
        for service in (self.watchers, self.schedules):
            try:
                service.stop()
            except Exception as error:
                logger.warning("stop failed", error=str(error))
        if self._server is not None:
            self._server.should_exit = True
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads = []
        self._server = None
        self.started = False
        logger.info("iris host stopped")

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        self.start()
        waiter = stop_event or threading.Event()
        try:
            while not waiter.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


__all__ = ["HEARTBEAT_SECONDS", "IrisHost", "probe_host"]
