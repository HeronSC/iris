# File: core/actions/bootstrap.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.actions.audit import ActionAuditLogger
from core.actions.changes import ChangeLedger
from core.actions.executor import ActionExecutionContext, ActionExecutor, SystemAdapter
from core.actions.implementations.add_document_root import AddDocumentRootAction
from core.actions.implementations.clipboard import ClipboardAction
from core.actions.implementations.active_context import ActiveContextAction
from core.actions.implementations.camera_tools import CAMERA_ACTIONS
from core.actions.implementations.code_tools import CODE_ACTIONS
from core.actions.implementations.excel_tools import EXCEL_ACTIONS
from core.actions.implementations.file_tools import FILE_ACTIONS
from core.actions.implementations.git_tools import GIT_ACTIONS
from core.actions.implementations.memory_tools import MEMORY_ACTIONS
from core.actions.implementations.network_tools import NETWORK_ACTIONS
from core.actions.implementations.project_tools import PROJECT_ACTIONS
from core.actions.implementations.fetch_web_page import FetchWebPageAction
from core.actions.implementations.web_search import WebSearchAction
from core.actions.implementations.launch_application import LaunchApplicationAction
from core.actions.implementations.open_file import OpenFileAction
from core.actions.implementations.open_folder import OpenFolderAction, ShowInExplorerAction
from core.actions.implementations.open_url import OpenUrlAction
from core.actions.implementations.scan_document_root import ScanDocumentRootAction
from core.actions.implementations.system_info import SYSTEM_ACTIONS
from core.actions.implementations.update_config import UpdateConfigAction
from core.actions.implementations.update_profile import UpdateProfileAction
from core.actions.models import ApplicationConfig
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.documents.models import DocumentSearchConfig
from core.permissions.policy import PermissionPolicy
from core.tools.registry import ToolRegistry
from core.web.fetch import PageFetcher
from core.web.search import DEFAULT_SEARCH_URL, SearchClient
from core.excel import ExcelService


@dataclass(frozen=True)
class ActionLayer:
    tool_registry: ToolRegistry
    action_registry: ActionRegistry
    executor: ActionExecutor
    page_fetcher: PageFetcher
    applications: dict[str, ApplicationConfig]
    app_alias_map: dict[str, str]
    audit: ActionAuditLogger


def document_search_config(config: dict[str, Any]) -> DocumentSearchConfig:
    document_cfg = config.get("document_search", {}) if isinstance(config.get("document_search"), dict) else {}
    return DocumentSearchConfig(
        roots=document_cfg.get("roots", []),
        directory_groups={str(key): set(value) for key, value in document_cfg.get("directory_groups", {}).items()},
        excluded_directories=set(document_cfg.get("excluded_directories", [])),
        supported_extensions=set(document_cfg.get("supported_extensions", [])),
        max_file_size_mb=int(document_cfg.get("max_file_size_mb", 100)),
        excluded_directory_prefixes=set(document_cfg.get("excluded_directory_prefixes", [])),
    )


def application_maps(config: dict[str, Any]) -> tuple[dict[str, ApplicationConfig], dict[str, str]]:
    applications_cfg = config.get("applications", {}) if isinstance(config.get("applications"), dict) else {}
    application_map: dict[str, ApplicationConfig] = {}
    alias_map: dict[str, str] = {}
    for app_id, payload in applications_cfg.items():
        if not isinstance(payload, dict):
            continue
        app = ApplicationConfig(
            id=app_id,
            display_name=str(payload.get("display_name", app_id)),
            executable=str(payload.get("executable", "")),
            aliases=[str(item).lower() for item in payload.get("aliases", []) if str(item).strip()],
        )
        application_map[app_id] = app
        alias_map[app_id.lower()] = app_id
        alias_map[app.display_name.lower()] = app_id
        for alias in app.aliases:
            alias_map[alias] = app_id
    return application_map, alias_map


def page_fetcher_from(config: dict[str, Any]) -> PageFetcher:
    web_cfg = config.get("web", {}) if isinstance(config.get("web"), dict) else {}
    general = config.get("general_knowledge", {}) if isinstance(config.get("general_knowledge"), dict) else {}
    return PageFetcher(
        user_agent=str(web_cfg.get("user_agent") or general.get("user_agent") or "Iris/1.0 (local desktop assistant)"),
        timeout_seconds=float(web_cfg.get("timeout_seconds", 15.0)),
        max_bytes=int(web_cfg.get("max_bytes", 3_000_000)),
        cache_ttl_seconds=float(web_cfg.get("cache_ttl_seconds", 600.0)),
    )


def documents_roots_for(config: dict[str, Any]) -> list[Path]:
    return document_search_config(config).root_paths()


def search_client_from(config: dict[str, Any]) -> SearchClient:
    web_cfg = config.get("web", {}) if isinstance(config.get("web"), dict) else {}
    general = config.get("general_knowledge", {}) if isinstance(config.get("general_knowledge"), dict) else {}
    return SearchClient(
        str(web_cfg.get("search_url") or DEFAULT_SEARCH_URL),
        timeout_seconds=float(web_cfg.get("search_timeout_seconds", 12.0)),
        cache_ttl_seconds=float(web_cfg.get("cache_ttl_seconds", 600.0)),
        language=str(web_cfg.get("search_language") or "en-US"),
        user_agent=str(web_cfg.get("user_agent") or general.get("user_agent") or "Iris/1.0 (local desktop assistant)"),
    )


def build_action_layer(
    config: dict[str, Any],
    *,
    catalog: Any,
    audit_folder: str | Path,
    permissions: PermissionPolicy | None = None,
    ledger: ChangeLedger | None = None,
    config_path: str | Path | None = None,
    memory_path: str | Path | None = None,
    scanner: Any = None,
    document_config: DocumentSearchConfig | None = None,
    tool_registry: ToolRegistry | None = None,
    context_service: Any = None,
    code_service: Any = None,
    project_service: Any = None,
    active_project_id: Any = None,
    excel_service: Any = None,
    knowledge: Any = None,
    cameras: Any = None,
) -> ActionLayer:
    registry_of_tools = tool_registry or ToolRegistry()
    action_registry = ActionRegistry(registry_of_tools)
    for action in (
        OpenFileAction(),
        OpenFolderAction(),
        ShowInExplorerAction(),
        LaunchApplicationAction(),
        OpenUrlAction(),
        ClipboardAction(),
        AddDocumentRootAction(),
        ScanDocumentRootAction(),
        UpdateConfigAction(),
        UpdateProfileAction(),
    ):
        action_registry.register(action)
    fetcher = page_fetcher_from(config)
    action_registry.register(FetchWebPageAction(fetcher))
    action_registry.register(WebSearchAction(search_client_from(config)))
    action_registry.register(ActiveContextAction())
    for code_action in CODE_ACTIONS:
        action_registry.register(code_action())
    for project_action in PROJECT_ACTIONS:
        action_registry.register(project_action())
    if excel_service is None:
        excel_service = ExcelService(documents_roots_for(config), context_service=context_service)
    for excel_action in EXCEL_ACTIONS:
        action_registry.register(excel_action())
    for system_action in SYSTEM_ACTIONS:
        action_registry.register(system_action())
    for network_action in NETWORK_ACTIONS:
        action_registry.register(network_action())
    for memory_action in MEMORY_ACTIONS:
        action_registry.register(memory_action())
    for file_action in FILE_ACTIONS:
        action_registry.register(file_action())
    for git_action in GIT_ACTIONS:
        action_registry.register(git_action())
    for camera_action in CAMERA_ACTIONS:
        action_registry.register(camera_action())

    documents = document_config or document_search_config(config)
    applications, alias_map = application_maps(config)
    audit = ActionAuditLogger(audit_folder)
    executor = ActionExecutor(
        registry=action_registry,
        policy=ActionPolicy(),
        audit=audit,
        permissions=permissions,
        ledger=ledger,
        context=ActionExecutionContext(
            catalog=catalog,
            allowed_roots=documents.root_paths(),
            applications=applications,
            app_alias_map=alias_map,
            web_shortcuts=dict(config.get("web_shortcuts", {}) or {}),
            system=SystemAdapter(),
            config_path=Path(config_path) if config_path else None,
            memory_path=Path(memory_path) if memory_path else None,
            document_scanner=scanner,
            context_service=context_service,
            code_service=code_service,
            project_service=project_service,
            active_project_id=active_project_id,
            excel_service=excel_service,
            knowledge=knowledge,
            cameras=cameras,
        ),
    )
    return ActionLayer(
        tool_registry=registry_of_tools,
        action_registry=action_registry,
        executor=executor,
        page_fetcher=fetcher,
        applications=applications,
        app_alias_map=alias_map,
        audit=audit,
    )


__all__ = ["ActionLayer", "application_maps", "build_action_layer", "document_search_config", "page_fetcher_from"]
