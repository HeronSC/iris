# File: core/application/service.py

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


from core.actions.audit import ActionAuditLogger
from core.actions.executor import ActionExecutionContext, ActionExecutor, SystemAdapter
from core.actions.implementations.add_document_root import AddDocumentRootAction
from core.actions.implementations.clipboard import ClipboardAction
from core.actions.implementations.fetch_web_page import FetchWebPageAction
from core.actions.implementations.system_info import SYSTEM_ACTIONS
from core.actions.implementations.launch_application import LaunchApplicationAction
from core.actions.implementations.open_file import OpenFileAction
from core.actions.implementations.open_folder import OpenFolderAction, ShowInExplorerAction
from core.actions.implementations.open_url import OpenUrlAction
from core.actions.implementations.scan_document_root import ScanDocumentRootAction
from core.actions.implementations.update_config import UpdateConfigAction
from core.actions.implementations.update_profile import UpdateProfileAction
from core.actions.models import ApplicationConfig
from core.actions.policy import ActionPolicy
from core.actions.registry import ActionRegistry
from core.application.contracts import (
    IrisEvent,
    ActionSuggestion,
    ConfirmationContent,
    ConversationContent,
    DetailContent,
    ErrorContent,
    IrisMessage,
    IrisResponse,
    IrisStatus,
    MessageRole,
    TopicContext,
)
from core.assistant.action_commands import ActionCommandHandler
from core.assistant.coordinator import AssistantCoordinator, CoordinatorTurn
from core.assistant.command_handler import CommandHandler
from core.assistant.conversation_synonyms import ConversationSynonymStore
from core.assistant.index_commands import IndexCommandHandler
from core.assistant.knowledge_commands import KnowledgeCommandHandler
from core.assistant.knowledge_provider import KnowledgeRecallProvider
from core.assistant.intent_example_store import IntentExampleStore
from core.assistant.intent_router import IntentRouter
from core.assistant.memory_commands import MemoryCommandHandler
from core.assistant.models_command import ModelsCommandHandler
from core.assistant.pending_action_manager import PendingActionManager
from core.assistant.project_command import ProjectCommandHandler
from core.assistant.proposal_commands import ProposalCommandHandler
from core.assistant.save_command import SaveCommandHandler
from core.assistant.search_commands import SearchCommandHandler
from core.assistant.session_commands import SessionCommandHandler
from core.assistant.tools_command import ToolsCommandHandler
from core.assistant.watch_command import WatchCommandHandler
from core.assistant.topic_commands import TopicCommandHandler
from core.assistant.prompting import PROMPT_CANCEL_TOKEN, PromptRequest, PromptType
from core.assistant.workflows import MemoryReviewWorkflow, SessionCloseWorkflow
from core.audit.logger import AuditLogger
from core.config.loader import ConfigError, ConfigLoader
from core.conversation.context_builder import ContextBuilder
from core.conversation.session import ConversationSession
from core.conversation.session_manager import SessionManager
from core.conversation.session_repository import SessionRepository
from core.conversation.persistent_memory import MemoryConfig, TopicMemoryService
from core.documents.catalog import DocumentCatalog
from core.documents.extractors.csv_extractor import CsvExtractor
from core.documents.extractors.docx_extractor import DocxExtractor
from core.documents.extractors.excel_extractor import ExcelExtractor
from core.documents.extractors.pdf_extractor import PdfExtractor
from core.documents.extractors.text_extractor import TextExtractor
from core.documents.models import DocumentSearchConfig
from core.documents.embeddings import DocumentEmbeddingConfig, DocumentEmbeddingIndex
from core.documents.ocr import OcrService, VisionOcr, WindowsOcr
from core.documents.query_parser import FileSearchQueryParser, QueryParserConfig
from core.documents.scanner import DocumentScanner
from core.documents.search_service import DocumentSearchService
from core.documents.watch import DocumentWatchService
from core.knowledge import EmbeddingConfig, KnowledgeGraph, KnowledgeRetriever, MemoryEmbeddingIndex
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.review import KnowledgeReviewWorkflow
from core.llm.metrics import RequestMetricsStore
from core.assistant.why_command import WhyCommandHandler
from core.observability import bind_request, clear_request, current_request_id, log_dir_for
from core.observability.logging_setup import LOG_FILE_NAME
from core.llm.ollama_client import OllamaClient, OllamaClientError
from core.llm.router import ModelRouter, ModelRoutes
from core.profile.loader import AssistantMemoryError, MemoryLoader
from core.profile.proposal_generator import MemoryProposalGenerator
from core.profile.proposal_reviewer import MemoryProposalReviewer
from core.profile.proposal_store import MemoryProposalStore
from core.profile.store import MemoryStore
from core.profile.update_service import MemoryUpdateService
from core.profile.writer import MemoryWriter
from core.state.search_result_context import SearchResultContext
from core.storage.sqlite_database import SQLiteDatabase
from core.system.applications import ApplicationCatalog
from core.system.places import KnownFolder, find_folders, root_subfolders, vscode_folders
from core.tools.mcp_client import McpManager, load_server_configs
from core.tools.models import PermissionLevel, ToolDefinition, ToolKind
from core.tools.registry import ToolRegistry
from core.watchers import InboxNotifier, LogNotifier, QuietHours, ToastNotifier, WatcherService
from core.web.fetch import PageFetcher

import structlog

logger = structlog.get_logger(__name__)


EventHandler = Callable[[IrisEvent], None]


class _NoopCommandHandler:
    def handle(self, user_input: str, state: dict[str, object]) -> bool:
        _ = user_input, state
        return False


class _NoopNaturalLanguageHandler(_NoopCommandHandler):
    def handle_natural_language(self, user_input: str) -> bool:
        _ = user_input
        return False


class IrisApplication:
    def __init__(self, config_path: Path | None = None, prompt_provider: Callable[[PromptRequest | str], str] | None = None) -> None:
        self.config_path = config_path or (Path(__file__).resolve().parents[1] / "config.json")
        self.prompt_provider = prompt_provider
        self._active_prompt_provider: Callable[[PromptRequest | str], str] | None = None
        self.event_handler: EventHandler | None = None
        self._active_event_handler: EventHandler | None = None
        self._streamed_message_counts: dict[tuple[MessageRole, str], int] = {}
        self._current_status: IrisStatus = IrisStatus.READY
        self._conversation_override_message: str | None = None
        self._detail_type_override: str | None = None
        self._detail_content_override: str | None = None
        self._detail_title_override: str | None = None
        self._detail_metadata_override: dict[str, Any] = {}
        self._detail_actions_override: list[ActionSuggestion] = []
        self._active_slash_command: str | None = None
        self._last_user_message: str = ""
        self._active_topic_id: str | None = None
        self._active_topic_title: str | None = None
        self._topic_counter = 0
        self._section_counter = 0
        self.initialized = False
        self.startup_messages: list[IrisMessage] = []
        self.topic_memory_service: TopicMemoryService | None = None
        self._last_coordinator_turn: CoordinatorTurn | None = None

        self.topic_handler: CommandHandler = _NoopCommandHandler()
        self.save_handler: CommandHandler = _NoopCommandHandler()
        self.session_handler: CommandHandler = _NoopCommandHandler()
        self.proposal_handler: CommandHandler = _NoopCommandHandler()
        self.memory_handler: CommandHandler = _NoopCommandHandler()
        self.index_handler: CommandHandler = _NoopCommandHandler()
        self.search_handler: CommandHandler = _NoopNaturalLanguageHandler()
        self.action_handler: CommandHandler = _NoopNaturalLanguageHandler()
        self.project_handler: CommandHandler = _NoopCommandHandler()
        self.knowledge_handler: CommandHandler = _NoopCommandHandler()
        self.tools_handler: CommandHandler = _NoopCommandHandler()
        self.models_handler: CommandHandler = _NoopCommandHandler()
        self.why_handler: CommandHandler = _NoopCommandHandler()
        self.watch_handler: CommandHandler = _NoopCommandHandler()
        self.last_request_id: str | None = None

    def initialize(self, event_handler: EventHandler | None = None) -> None:
        self.event_handler = event_handler
        self._response_messages: list[IrisMessage] = []
        try:
            self.config_loader = ConfigLoader(self.config_path)
            self.config = self.config_loader.load()
            memory_loader = MemoryLoader(self.config["memory_path"])
            memory_data = memory_loader.load()
        except (ConfigError, AssistantMemoryError) as error:
            self._emit_message(MessageRole.ERROR, f"Assistant could not start: {error}", IrisStatus.ERROR)
            raise

        self.store = MemoryStore(memory_data)
        self.writer = MemoryWriter(self.config["memory_path"])
        context_builder = ContextBuilder(self.config["assistant_name"], self.store)
        ollama_client = OllamaClient(
            self.config["llm_server"],
            self.config["model"],
            timeout_seconds=self.config.get("llm_timeout_seconds", 30.0),
        )
        metrics_path = self.config.get("metrics_path") or Path(self.config["memory_path"]).parent / "Metrics" / "metrics.db"
        try:
            self.request_metrics: RequestMetricsStore | None = RequestMetricsStore(SQLiteDatabase(metrics_path))
        except Exception as error:
            logger.warning("Model metrics disabled: %s", error)
            self.request_metrics = None
        self.model_router = ModelRouter(
            ollama_client,
            ModelRoutes.from_config(self.config),
            metrics=self.request_metrics,
            request_id_provider=current_request_id,
        )
        self.ollama_client = self.model_router
        session = ConversationSession(max_messages=8)
        self.session_repository = SessionRepository(
            self.config.get("session_path") or Path(__file__).resolve().parents[1] / "sessions",
            assistant_name=self.config["assistant_name"],
            model=self.config["model"],
        )
        self.session_manager = SessionManager(self.session_repository)
        conversation_config = self.config.get("conversation", {}) if isinstance(self.config, dict) else {}
        resume_last_session = bool(conversation_config.get("resume_last_session", False)) if isinstance(conversation_config, dict) else False
        if resume_last_session:
            recent_session_id = self.session_manager.get_most_recent_session_id()
            if recent_session_id is not None:
                self.session_manager.resume_session(recent_session_id)
            else:
                self.session_manager.start_session(title="New Session")
        else:
            self.session_manager.start_session(title="New Session")

        self.coordinator = AssistantCoordinator(
            self.config["assistant_name"],
            self.store,
            self.config,
            context_builder=context_builder,
            ollama_client=self.ollama_client,
            session=session,
            session_manager=self.session_manager,
        )

        memory_config = MemoryConfig.from_config(self.config)
        if memory_config.enabled:
            self.topic_memory_service = TopicMemoryService(memory_config)
            self.topic_handler = TopicCommandHandler(self.topic_memory_service, output=self._sink)
            self.coordinator.topic_memory_service = self.topic_memory_service
        else:
            self.topic_memory_service = None
            self.topic_handler = _NoopCommandHandler()

        knowledge_path = self.config.get("knowledge_path") or memory_config.database_path.parent / "knowledge.db"
        knowledge_database = SQLiteDatabase(knowledge_path)
        self.knowledge = KnowledgeGraph(knowledge_database)
        self.hypotheses = HypothesisTracker(self.knowledge)
        self.embedding_index = MemoryEmbeddingIndex(
            knowledge_database,
            self.model_router,
            EmbeddingConfig.from_config(self.config),
        )
        self.knowledge_retriever = KnowledgeRetriever(knowledge_database, embeddings=self.embedding_index)

        knowledge_cfg = self.config.get("general_knowledge", {}) if isinstance(self.config, dict) else {}
        recall_enabled = bool((knowledge_cfg.get("enabled", {}) or {}).get("recall", True))
        if recall_enabled:
            self.coordinator.general_knowledge_router.register(
                KnowledgeRecallProvider(self.knowledge_retriever)
            )

        self.project_handler: CommandHandler = ProjectCommandHandler(output=self._sink)
        self.save_handler: CommandHandler = SaveCommandHandler(output=self._sink)
        proposal_store = MemoryProposalStore(self.config.get("proposal_path") or Path(__file__).resolve().parents[1] / "memory" / "proposals")
        audit_logger = AuditLogger(self.config.get("audit_path") or Path(__file__).resolve().parents[1] / "audit")
        self.knowledge_review = KnowledgeReviewWorkflow(self.hypotheses, audit_logger=audit_logger)
        self.knowledge_handler: CommandHandler = KnowledgeCommandHandler(
            self.knowledge_review,
            output=self._sink,
            actor=str(self.config.get("assistant_user", "user")),
            embeddings=self.embedding_index,
        )
        proposal_generator = MemoryProposalGenerator(self.ollama_client)
        reviewer = MemoryProposalReviewer()
        update_service = MemoryUpdateService(self.config["memory_path"], proposal_store, audit_logger, memory_store=self.store)
        close_workflow = SessionCloseWorkflow(
            session_manager=self.session_manager,
            proposal_store=proposal_store,
            proposal_generator=proposal_generator,
            config=self.config,
            store=self.store,
        )
        review_workflow = MemoryReviewWorkflow(
            session_manager=self.session_manager,
            proposal_store=proposal_store,
            proposal_generator=proposal_generator,
            reviewer=reviewer,
            update_service=update_service,
            store=self.store,
        )
        self.session_handler: CommandHandler = SessionCommandHandler(
            self.session_manager,
            self.session_repository,
            close_workflow=close_workflow,
            output=self._sink,
        )
        self.memory_handler: CommandHandler = MemoryCommandHandler(
            proposal_store,
            review_workflow=review_workflow,
            output=self._sink,
        )
        self.proposal_handler: CommandHandler = ProposalCommandHandler(
            proposal_store,
            session_manager=self.session_manager,
            proposal_generator=proposal_generator,
            output=self._sink,
        )

        document_cfg = self.config.get("document_search", {}) if isinstance(self.config, dict) else {}
        document_config = DocumentSearchConfig(
            roots=document_cfg.get("roots", []),
            directory_groups={
                str(key): set(value)
                for key, value in document_cfg.get("directory_groups", {}).items()
            },
            excluded_directories=set(document_cfg.get("excluded_directories", [])),
            supported_extensions=set(document_cfg.get("supported_extensions", [])),
            max_file_size_mb=int(document_cfg.get("max_file_size_mb", 100)),
            excluded_directory_prefixes=set(document_cfg.get("excluded_directory_prefixes", [])),
        )
        document_db = SQLiteDatabase(document_cfg.get("catalog_path") or Path(__file__).resolve().parents[1] / "index" / "documents.db")
        document_catalog = DocumentCatalog(document_db)
        scanner = DocumentScanner(
            config=document_config,
            catalog=document_catalog,
            extractors=[
                TextExtractor(),
                CsvExtractor(),
                DocxExtractor(),
                ExcelExtractor(),
                PdfExtractor(ocr=self._build_ocr()),
            ],
        )
        parser = FileSearchQueryParser(QueryParserConfig(default_roots=document_config.root_paths()))
        self.document_embeddings = DocumentEmbeddingIndex(document_db, self.model_router, DocumentEmbeddingConfig.from_config(self.config))
        document_search_service = DocumentSearchService(document_catalog, parser, embeddings=self.document_embeddings)
        watch_cfg = document_cfg.get("watch", {}) if isinstance(document_cfg.get("watch"), dict) else {}
        self.document_watch: DocumentWatchService | None = None
        if bool(watch_cfg.get("enabled", True)) and DocumentWatchService.available():
            self.document_watch = DocumentWatchService(
                scanner,
                debounce_seconds=float(watch_cfg.get("debounce_seconds", 2.0)),
                rescan_interval_hours=float(watch_cfg.get("rescan_interval_hours", 24.0)),
            )
            try:
                self.document_watch.start()
            except Exception as error:
                logger.warning("Document change watching could not start", error=str(error))
                self.document_watch = None
        self.index_handler: CommandHandler = IndexCommandHandler(
            scanner,
            document_catalog,
            config_path=self.config_loader.config_path,
            output=self._sink,
            prompt_provider=self._prompt,
            watch_service=self.document_watch,
        )
        search_context = SearchResultContext(ttl_minutes=30)
        self.search_handler = SearchCommandHandler(document_search_service, search_context=search_context, output=self._sink)

        applications_cfg = self.config.get("applications", {}) if isinstance(self.config, dict) else {}
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

        self.tool_registry = ToolRegistry()
        action_registry = ActionRegistry(self.tool_registry)
        action_registry.register(OpenFileAction())
        action_registry.register(OpenFolderAction())
        action_registry.register(ShowInExplorerAction())
        action_registry.register(LaunchApplicationAction())
        action_registry.register(OpenUrlAction())
        action_registry.register(ClipboardAction())
        action_registry.register(AddDocumentRootAction())
        action_registry.register(ScanDocumentRootAction())
        action_registry.register(UpdateConfigAction())
        action_registry.register(UpdateProfileAction())
        web_cfg = self.config.get("web", {}) if isinstance(self.config.get("web"), dict) else {}
        self.page_fetcher = PageFetcher(
            user_agent=str(web_cfg.get("user_agent") or (self.config.get("general_knowledge", {}) or {}).get("user_agent") or "Iris/1.0 (local desktop assistant)"),
            timeout_seconds=float(web_cfg.get("timeout_seconds", 15.0)),
            max_bytes=int(web_cfg.get("max_bytes", 3_000_000)),
            cache_ttl_seconds=float(web_cfg.get("cache_ttl_seconds", 600.0)),
        )
        action_registry.register(FetchWebPageAction(self.page_fetcher))
        for system_action in SYSTEM_ACTIONS:
            action_registry.register(system_action())

        action_audit = ActionAuditLogger(self.config.get("action_audit_path") or Path(__file__).resolve().parents[1] / "audit")
        self.action_executor = ActionExecutor(
            registry=action_registry,
            policy=ActionPolicy(),
            audit=action_audit,
            context=ActionExecutionContext(
                catalog=document_catalog,
                allowed_roots=document_config.root_paths(),
                applications=application_map,
                app_alias_map=alias_map,
                web_shortcuts=self.config.get("web_shortcuts", {}) if isinstance(self.config, dict) else {},
                system=SystemAdapter(),
                config_path=self.config_loader.config_path,
                memory_path=Path(self.config["memory_path"]),
                document_scanner=scanner,
            ),
        )
        self.application_catalog = ApplicationCatalog()
        document_roots = document_config.root_paths()

        def _find_known_folders(name: str) -> list[KnownFolder]:
            candidates = vscode_folders() + root_subfolders(document_roots)
            return find_folders(name, candidates)

        self.action_handler = ActionCommandHandler(
            self.action_executor,
            action_audit,
            search_context,
            output=self._sink,
            prompt_provider=self._prompt,
            application_catalog=self.application_catalog,
            folder_finder=_find_known_folders,
        )
        conversation_synonyms = ConversationSynonymStore(self.config["memory_path"].parent / "Configuration" / "conversation_synonyms.json")
        self.pending_action_manager = PendingActionManager(
            self.action_executor,
            synonyms=conversation_synonyms,
            phrase_log_path=Path(self.config["audit_path"] or self.config["memory_path"].parent / "Audit") / "conversation_phrases.jsonl",
            output=self._sink,
        )
        intent_example_store = IntentExampleStore(self.config_loader.config_path.parent / "intent_examples.json")
        self.intent_router = IntentRouter(
            llm_client=self.ollama_client,
            index_handler=self.index_handler,
            memory_handler=self.memory_handler,
            action_executor=self.action_executor,
            example_store=intent_example_store,
            conversation_synonyms=conversation_synonyms,
            output=self._sink,
            tool_registry=self.tool_registry,
        )
        self._register_capability_tools()
        self.coordinator.attach_tools(
            tool_registry=self.tool_registry,
            action_executor=self.action_executor,
            example_store=intent_example_store,
            checkpoint_path=Path(self.config.get("session_path") or Path(self.config["memory_path"]).parent / "Sessions") / "agent_checkpoints.db",
        )
        self.mcp_manager = McpManager(
            load_server_configs(self.config.get("mcp_servers") if isinstance(self.config, dict) else None),
            action_registry,
            on_event=lambda message: logger.warning(message),
        )
        self.mcp_manager.start(background=True)
        self.tools_handler = ToolsCommandHandler(self.tool_registry, self.mcp_manager, output=self._sink)
        self.models_handler = ModelsCommandHandler(self.model_router, self.request_metrics, output=self._sink)
        self.watchers = self._build_watchers()
        self.watch_handler = WatchCommandHandler(self.watchers, output=self._sink)
        self.why_handler = WhyCommandHandler(
            log_file=log_dir_for(self.config) / LOG_FILE_NAME,
            trace_file=getattr(getattr(self.coordinator, "trace_logger", None), "path", None),
            metrics=self.request_metrics,
            action_audit=action_audit,
            last_request_id=lambda: getattr(self, "last_request_id", None),
            output=self._sink,
        )
        threading.Thread(target=self._report_model_route_warnings, name="model-route-check", daemon=True).start()
        self._embedding_stop = threading.Event()
        self._embedding_thread = threading.Thread(target=self._embedding_catch_up, name="embeddings", daemon=True)
        self._embedding_thread.start()

        active_session = self.session_manager.get_active_session()
        active_project_id = active_session.project_id if active_session is not None else None
        self.state = {
            "assistant_name": self.config["assistant_name"],
            "active_project_id": active_project_id,
            "store": self.store,
            "writer": self.writer,
            "session_manager": self.session_manager,
        }

        self.startup_messages = [
            IrisMessage(MessageRole.SYSTEM, "Assistant memory loaded."),
            IrisMessage(MessageRole.SYSTEM, f"Configured Ollama model: {self.config['model']}"),
            IrisMessage(MessageRole.SYSTEM, f"Ollama endpoint: {self.config['llm_server']}"),
            IrisMessage(MessageRole.SYSTEM, "Iris is ready."),
        ]
        self.initialized = True
        if self.event_handler is not None:
            for message in self.startup_messages:
                self.event_handler(IrisEvent(status=IrisStatus.READY, message=message))

    def process_message(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
        event_handler: EventHandler | None = None,
        prompt_provider: Callable[[PromptRequest | str], str] | None = None,
    ) -> IrisResponse:
        if not self.initialized:
            raise RuntimeError("IrisApplication is not initialized")

        self._active_event_handler = event_handler if event_handler is not None else self.event_handler
        self._active_prompt_provider = prompt_provider if prompt_provider is not None else self.prompt_provider
        self._streamed_message_counts = {}
        self._conversation_override_message = None
        self._detail_type_override = None
        self._detail_content_override = None
        self._detail_title_override = None
        self._detail_metadata_override = {}
        self._detail_actions_override = []
        self._active_slash_command = None

        stripped = text.strip()
        self._last_user_message = stripped
        if not stripped:
            return IrisResponse(messages=[], status=IrisStatus.COMPLETE, conversation=ConversationContent(message=""))
        if cancel_event is not None and cancel_event.is_set():
            cancelled_message = IrisMessage(MessageRole.SYSTEM, "Request cancelled.")
            return IrisResponse(
                messages=[cancelled_message],
                status=IrisStatus.CANCELLED,
                conversation=ConversationContent(message=cancelled_message.text),
                details=DetailContent(type="error", title="Cancelled", summary=cancelled_message.text, items=[{"role": cancelled_message.role.value, "text": cancelled_message.text}]),
                metadata={"cancelled": True},
            )

        self._response_messages = [IrisMessage(MessageRole.USER, stripped)]
        status = self._status_for_input(stripped)
        self._current_status = status
        self._emit_event(status)
        self.last_request_id = bind_request()
        self._turn_started = time.perf_counter()
        self._turn_route = "coordinator"

        if stripped.lower() in {"exit", "quit"}:
            self.session_handler.handle("/session close", self.state)
            return IrisResponse(
                messages=self._response_messages,
                status=IrisStatus.COMPLETE,
                response_type="session",
                conversation=self._build_conversation_content(self._response_messages, response_type="session"),
                details=DetailContent(
                    type="command_output",
                    title="Session closed",
                    summary="Iris closed the current session.",
                    items=[{"role": message.role.value, "text": message.text} for message in self._response_messages if message.role != MessageRole.USER],
                    metadata={"close_application": True},
                ),
                metadata={"close_application": True},
            )

        self.state["cancel_event"] = cancel_event

        try:
            awaiting_method = getattr(getattr(self, "coordinator", None), "awaiting_confirmation", None)
            awaiting = awaiting_method() if callable(awaiting_method) else None
            if awaiting is not None:
                mapped = self.pending_action_manager._resolve_pending_response(stripped)
                if mapped in {"confirm", "cancel"}:
                    self._turn_route = "confirmation"
                    turn = self.coordinator.resume_confirmation(mapped == "confirm", on_delta=self._emit_delta if self._active_event_handler is not None else None)
                    if turn is not None:
                        self._emit_message(MessageRole.ASSISTANT, turn.text)
                        return self._build_response(IrisStatus.COMPLETE, cancel_event)
                elif not self.pending_action_manager._looks_like_modification(stripped):
                    self._emit_message(
                        MessageRole.CONFIRMATION,
                        "You still have an action awaiting confirmation:\n\n"
                        f"{awaiting.get('summary') or awaiting.get('message') or 'An action is awaiting confirmation.'}\n\n"
                        "Reply naturally to confirm or cancel, or tell me what to change.",
                    )
                    return self._build_response(IrisStatus.AWAITING_CONFIRMATION, cancel_event)
                else:
                    self.coordinator.resume_confirmation(False)
            if self.pending_action_manager.handle(stripped):
                return self._build_response(status, cancel_event)
            if stripped == "/conversations":
                stripped = "/session list"
            if stripped == "/new":
                stripped = "/session new"
            if stripped.lower().startswith("/resume "):
                stripped = "/session resume " + stripped.split(maxsplit=1)[1]
            if self._handle_slash_command(self.save_handler, stripped, status, command_prefixes=("/save",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.session_handler, stripped, status, command_prefixes=("/session",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.topic_handler, stripped, status, command_prefixes=("/topic",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.proposal_handler, stripped, status, command_prefixes=("/proposal",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.memory_handler, stripped, status, command_prefixes=("/memory",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.knowledge_handler, stripped, status, command_prefixes=("/knowledge",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.tools_handler, stripped, status, command_prefixes=("/tools",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.models_handler, stripped, status, command_prefixes=("/models",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.why_handler, stripped, status, command_prefixes=("/why",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.watch_handler, stripped, status, command_prefixes=("/watch",)):
                return self._build_response(status, cancel_event)
            if self._handle_slash_command(self.index_handler, stripped, IrisStatus.INDEXING, command_prefixes=("/index",)):
                return self._build_response(IrisStatus.INDEXING, cancel_event)
            if self._handle_slash_command(self.search_handler, stripped, IrisStatus.SEARCHING, command_prefixes=("/search", "/file")):
                return self._build_response(IrisStatus.SEARCHING, cancel_event)
            if self._handle_slash_command(
                self.action_handler,
                stripped,
                status,
                command_prefixes=(
                    "/confirm",
                    "/cancel",
                    "/open ",
                    "/show ",
                    "/folder ",
                    "/open-folder ",
                    "/copy path ",
                    "/launch ",
                    "/open-url ",
                    "/actions recent",
                ),
            ):
                return self._build_response(status, cancel_event)
            if self._should_route_to_topic_memory(stripped):
                return self._respond_with_coordinator(stripped, cancel_event)
            if self._should_prefer_coordinator_routing(stripped):
                return self._respond_with_coordinator(stripped, cancel_event)
            if self.intent_router.handle(stripped, self.state):
                self._turn_route = "intent"
                return self._build_response(status, cancel_event)
            if self.action_handler.handle_natural_language(stripped):
                self._turn_route = "action"
                return self._build_response(status, cancel_event)
            if self.search_handler.handle_natural_language(stripped):
                self._turn_route = "search"
                return self._build_response(IrisStatus.SEARCHING, cancel_event)
            if self._handle_slash_command(self.project_handler, stripped, status, command_prefixes=("/project",)):
                return self._build_response(status, cancel_event)

            return self._respond_with_coordinator(stripped, cancel_event)
        except OllamaClientError as error:
            self._emit_message(MessageRole.ERROR, f"Assistant error: {error}")
            return self._build_response(IrisStatus.ERROR, cancel_event)
        finally:
            self._log_turn(stripped)
            clear_request()
            self.state.pop("cancel_event", None)
            self._active_event_handler = None
            self._active_prompt_provider = None
            self._conversation_override_message = None
            self._detail_type_override = None
            self._detail_content_override = None
            self._detail_title_override = None
            self._detail_metadata_override = {}
            self._detail_actions_override = []
            self._active_slash_command = None

    def _log_turn(self, user_message: str) -> None:
        try:
            roles = self._message_role_counts()
            logger.info(
                "turn",
                route=self._turn_route,
                status=self._current_status.value,
                elapsed_ms=round((time.perf_counter() - self._turn_started) * 1000, 1),
                user_message=user_message[:200],
                replies=int(roles.get(MessageRole.ASSISTANT, 0)),
                errors=int(roles.get(MessageRole.ERROR, 0)),
            )
        except Exception:
            pass

    def _embedding_catch_up(self) -> None:
        indexes = [self.embedding_index, self.document_embeddings]
        interval = max(10.0, float(self.embedding_index.config.refresh_seconds))
        first = True
        while not self._embedding_stop.is_set():
            for index in indexes:
                if index.available:
                    try:
                        stored = index.index_pending()
                        if stored:
                            logger.info("embeddings", index=index.name, stored=stored, vectors=index.count())
                    except Exception as error:
                        logger.warning("Embedding pass failed", index=index.name, error=str(error))
                elif first:
                    logger.info("Embedding retrieval is off", index=index.name, status=index.status())
            first = False
            if self._embedding_stop.wait(interval):
                break

    def _build_watchers(self) -> WatcherService:
        configuration = Path(self.config["memory_path"]).parent / "Configuration"
        audit = Path(self.config.get("audit_path") or Path(self.config["memory_path"]).parent / "Audit")
        notifications_cfg = self.config.get("notifications", {}) if isinstance(self.config.get("notifications"), dict) else {}
        notifiers: dict[str, Any] = {"log": LogNotifier()}
        if ToastNotifier.available():
            notifiers["toast"] = ToastNotifier(str(self.config.get("assistant_name", "Iris")))
        try:
            quiet = QuietHours.parse(str(notifications_cfg.get("quiet_hours") or ""))
        except ValueError as error:
            logger.warning("Ignoring notifications.quiet_hours", error=str(error))
            quiet = QuietHours()
        service = WatcherService(
            configuration / "watchers.json",
            configuration / "watchers_state.json",
            notifiers,
            quiet_hours=quiet,
            inbox=InboxNotifier(audit / "notifications.jsonl"),
        )
        if bool(notifications_cfg.get("enabled", True)):
            try:
                service.start()
            except Exception as error:
                logger.warning("Watchers could not start", error=str(error))
        return service

    def _build_ocr(self) -> OcrService | None:
        readers: list[Any] = []
        if WindowsOcr.available():
            readers.append(WindowsOcr())
        readers.append(VisionOcr(self.model_router))
        service = OcrService(readers)
        return service if service.available else None

    def _report_model_route_warnings(self) -> None:
        try:
            for warning in self.model_router.check_routes():
                logger.warning("Model routes: %s", warning)
        except Exception as error:
            logger.warning("Model route check failed: %s", error)

    def _register_capability_tools(self) -> None:
        router = getattr(self.coordinator, "general_knowledge_router", None)
        if router is None:
            return
        for provider in getattr(router, "providers", []):
            definition = provider.definition() if hasattr(provider, "definition") else None
            if definition is None or definition.name in self.tool_registry:
                continue
            self.tool_registry.register(
                ToolDefinition(
                    name=definition.name,
                    description=definition.description,
                    parameters=dict(definition.request_schema),
                    permission=PermissionLevel.READ,
                    kind=ToolKind.CAPABILITY,
                    expose_to_model=False,
                    source="knowledge",
                    handler=provider,
                )
            )

    def shutdown(self) -> None:
        if not self.initialized:
            return
        try:
            self.session_handler.handle("/session close", self.state)
        except Exception:
            pass
        stop = getattr(self, "_embedding_stop", None)
        if stop is not None:
            stop.set()
        watchers = getattr(self, "watchers", None)
        if watchers is not None:
            try:
                watchers.stop()
            except Exception:
                pass
        document_watch = getattr(self, "document_watch", None)
        if document_watch is not None:
            try:
                document_watch.stop()
            except Exception:
                pass
        mcp_manager = getattr(self, "mcp_manager", None)
        if mcp_manager is not None:
            try:
                mcp_manager.stop()
            except Exception:
                pass
        self.initialized = False

    def _status_for_input(self, user_input: str) -> IrisStatus:
        lowered = user_input.lower()
        if lowered.startswith("/index"):
            return IrisStatus.INDEXING
        if lowered.startswith("/search") or lowered.startswith("/file"):
            return IrisStatus.SEARCHING
        return IrisStatus.THINKING

    def _handle_slash_command(
        self,
        handler: CommandHandler,
        user_input: str,
        status: IrisStatus,
        *,
        command_prefixes: tuple[str, ...],
    ) -> bool:
        if not user_input.startswith("/"):
            return False
        lowered = user_input.lower()
        if not any(lowered.startswith(prefix) for prefix in command_prefixes):
            return False

        role_counts_before = self._message_role_counts()
        command_name = self._command_name(user_input)
        self._emit_message(MessageRole.SYSTEM, f"{command_name} started.", status=status)
        handled = handler.handle(user_input, self.state)
        if not handled:
            return False

        self._turn_route = f"command:{command_name}"
        role_counts_after = self._message_role_counts()
        error_delta = role_counts_after.get(MessageRole.ERROR, 0) - role_counts_before.get(MessageRole.ERROR, 0)
        completion_text = f"{command_name} completed with errors." if error_delta > 0 else f"{command_name} complete."
        self._emit_message(MessageRole.SYSTEM, completion_text, status=IrisStatus.COMPLETE)

        self._active_slash_command = user_input
        self._conversation_override_message = completion_text
        self._detail_type_override = "command_output"
        self._detail_title_override = f"{command_name} details"
        self._detail_metadata_override["command"] = user_input
        return True

    def _message_role_counts(self) -> dict[MessageRole, int]:
        counts: dict[MessageRole, int] = {}
        for message in self._response_messages:
            counts[message.role] = counts.get(message.role, 0) + 1
        return counts

    def _command_name(self, user_input: str) -> str:
        stripped = user_input.strip()
        token = stripped.split()[0] if stripped else ""
        if token.startswith("/"):
            token = token[1:]
        token = token.replace("_", " ").strip()
        if not token:
            return "Command"
        return " ".join(part.capitalize() for part in token.split())

    def _should_route_to_topic_memory(self, user_input: str) -> bool:
        text = (user_input or "").strip()
        if not text or text.startswith("/"):
            return False
        if self.topic_memory_service is None or not self.topic_memory_service.config.enabled:
            return False
        lowered = text.lower()
        mutation_markers = (
            "remove ",
            "reject ",
            "do not want ",
            "don't want ",
            "only keep",
            "exclude ",
            "restore ",
            "put back",
        )
        if not any(marker in lowered for marker in mutation_markers):
            return False
        admin_markers = (
            "web shortcut",
            "document root",
            "config",
            "application",
            "profile",
            "setting",
            "index",
        )
        if any(marker in lowered for marker in admin_markers):
            return False
        return True

    def _should_prefer_coordinator_routing(self, user_input: str) -> bool:
        text = (user_input or "").strip()
        if not text or text.startswith("/"):
            return False

        request_pipeline = getattr(self.coordinator, "request_pipeline", None)
        if request_pipeline is None or not hasattr(request_pipeline, "build_request"):
            return False

        session = getattr(self.coordinator, "session", None)
        state = {
            "project_id": self.state.get("active_project_id"),
            "session": session,
        }
        try:
            request = request_pipeline.build_request(text, state=state)
        except (RuntimeError, OSError, ValueError, TypeError, AssertionError):
            return False

        intent = str(getattr(request, "intent", "")).strip().lower()
        requires_tool = bool(getattr(request, "requires_tool", False))
        if not requires_tool:
            return False
        return intent in {"find_files", "read_file", "count_files", "select_pending_result"}

    def _emit_delta(self, text: str) -> None:
        if self._active_event_handler is not None and text:
            self._active_event_handler(IrisEvent(status=IrisStatus.THINKING, delta=text))

    def _respond_with_coordinator(self, stripped: str, cancel_event: threading.Event | None) -> IrisResponse:
        turn = self.coordinator.respond_detailed(
            stripped,
            project_id=self.state.get("active_project_id"),
            on_delta=self._emit_delta if self._active_event_handler is not None else None,
            cancel_event=cancel_event,
        )
        self._last_coordinator_turn = turn
        response_text = turn.text
        self._detail_type_override = "markdown"
        self._detail_content_override = response_text
        self._detail_title_override = self._derive_detail_title(stripped)
        self._detail_metadata_override = {"render_operation": "replace_section"}
        general_knowledge = turn.general_knowledge
        if general_knowledge:
            detail_type = str(general_knowledge.get("detail_type", "")).strip()
            detail_title = str(general_knowledge.get("detail_title", "")).strip()
            detail_content = general_knowledge.get("detail_content")
            metadata = general_knowledge.get("metadata") if isinstance(general_knowledge.get("metadata"), dict) else {}
            if detail_type:
                self._detail_type_override = detail_type
            if detail_title:
                self._detail_title_override = detail_title
            if isinstance(detail_content, str) and detail_content.strip():
                self._detail_content_override = detail_content
            self._detail_metadata_override.update(metadata)
            facts_detail = self._render_capability_details_from_facts(self._detail_metadata_override)
            if facts_detail:
                self._detail_content_override = facts_detail
        file_operations_payload = turn.file_operations
        if file_operations_payload:
            self._detail_metadata_override["file_operations"] = file_operations_payload
            if self._detail_type_override in {None, "text", "markdown"}:
                self._detail_type_override = "search_results"
            if not self._detail_title_override:
                self._detail_title_override = "File search results"
        actions_method = getattr(self.coordinator, "suggest_follow_up_actions", None)
        if (
            callable(actions_method)
            and not (cancel_event is not None and cancel_event.is_set())
            and self._follow_up_actions_allowed(response_text)
        ):
            try:
                generated_actions = actions_method(
                    user_message=stripped,
                    detailed_response=response_text,
                    topic_title=self._active_topic_title,
                )
            except (RuntimeError, OSError, ValueError, TypeError, AssertionError):
                generated_actions = []
            if isinstance(generated_actions, list):
                self._detail_actions_override = [action for action in generated_actions if isinstance(action, ActionSuggestion)]
        summarize_method = getattr(self.coordinator, "summarize_for_chat", None)
        if callable(summarize_method) and not (cancel_event is not None and cancel_event.is_set()):
            try:
                conversation_text = str(
                    summarize_method(
                        user_message=stripped,
                        detailed_response=response_text,
                        project_id=self.state.get("active_project_id"),
                    )
                ).strip()
            except (RuntimeError, OSError, ValueError, TypeError, AssertionError):
                conversation_text = ""
            if conversation_text:
                self._conversation_override_message = conversation_text
        self._emit_message(MessageRole.ASSISTANT, response_text)
        return self._build_response(IrisStatus.COMPLETE, cancel_event)

    def _derive_detail_title(self, user_message: str) -> str:
        title = self._extract_topic_title(user_message)
        if title:
            return title
        return "Supporting details"

    def _render_capability_details_from_facts(self, metadata: dict[str, Any]) -> str:
        capability = str(metadata.get("capability", "")).strip().lower()
        facts = metadata.get("facts") if isinstance(metadata.get("facts"), dict) else {}
        if not capability or not facts:
            return ""

        lines: list[str] = []
        if capability == "stocks":
            ticker = str(facts.get("ticker", "")).strip()
            price = facts.get("price")
            open_value = facts.get("open")
            change = facts.get("change")
            percent = facts.get("percent_change")
            lines.append(f"## Stock quote: {ticker or 'Unknown'}")
            if isinstance(price, (int, float)):
                lines.append(f"- Last price: ${float(price):.2f}")
            if isinstance(open_value, (int, float)):
                lines.append(f"- Open: ${float(open_value):.2f}")
            if isinstance(change, (int, float)) and isinstance(percent, (int, float)):
                lines.append(f"- Change: {float(change):+.2f} ({float(percent):+.2f}%)")
        elif capability == "news":
            topic = str(facts.get("topic", "")).strip() or "general"
            headlines = facts.get("headlines") if isinstance(facts.get("headlines"), list) else []
            lines.append(f"## Top headlines ({topic})")
            for item in headlines:
                text = str(item).strip()
                if text:
                    lines.append(f"- {text}")
        elif capability == "time":
            timezone_value = str(facts.get("timezone", "")).strip()
            date_time = str(facts.get("datetime", "")).strip()
            lines.append(f"## Current time in {timezone_value or 'selected timezone'}")
            if date_time:
                lines.append(f"- Datetime: {date_time}")
        elif capability == "lookup":
            query = str(facts.get("query", "")).strip()
            answer = str(facts.get("answer", "")).strip()
            lines.append("## Lookup")
            if query:
                lines.append(f"- Query: {query}")
            if answer:
                lines.append(f"- Result: {answer}")
        elif capability == "weather":
            location = str(facts.get("location", "")).strip()
            range_name = str(facts.get("range", "")).strip().lower() or "current"
            headline = str(facts.get("headline", "")).strip()
            detail_lines = facts.get("details") if isinstance(facts.get("details"), list) else []
            condition = str(facts.get("condition", "")).strip()
            current_temp = str(facts.get("current_temp_f", "")).strip()
            feels_like = str(facts.get("feels_like_f", "")).strip()
            rain_summary = str(facts.get("rain_summary", "")).strip()
            weather_title_map = {
                "current": "Current weather",
                "today": "Today's weather",
                "tomorrow": "Tomorrow's forecast",
                "week": "Weekly outlook",
                "weekend": "Weekend forecast",
                "next_week": "Next week forecast",
            }
            title_prefix = weather_title_map.get(range_name, "Weather")
            lines.append(f"## {title_prefix} in {location or 'Current location'}")
            if headline:
                lines.append(f"- Summary: {headline}")

            if range_name in {"current", "today"}:
                if condition:
                    lines.append(f"- Conditions: {condition}")
                if current_temp:
                    lines.append(f"- Current temperature: {current_temp}")
                if feels_like:
                    lines.append(f"- Feels like: {feels_like}")
                if rain_summary:
                    lines.append(f"- Rain outlook: {rain_summary}")
            else:
                normalized_details = [str(item).strip() for item in detail_lines if str(item).strip()]
                if normalized_details:
                    lines.append("- Forecast details:")
                    for item in normalized_details:
                        text = item.lstrip("- ").strip()
                        if text:
                            lines.append(f"  - {text}")
        else:
            return ""

        source = str(metadata.get("source", "")).strip()
        retrieved_at = str(metadata.get("retrieved_at", "")).strip()
        valid_until = str(metadata.get("valid_until", "")).strip()
        coverage = metadata.get("coverage") if isinstance(metadata.get("coverage"), dict) else {}
        coverage_summary = str(coverage.get("summary", "")).strip() if isinstance(coverage, dict) else ""
        warnings = metadata.get("warnings") if isinstance(metadata.get("warnings"), list) else []
        warning_lines = [str(item).strip() for item in warnings if str(item).strip()]

        if source or retrieved_at or valid_until or coverage_summary or warning_lines:
            lines.append("")
        if source:
            lines.append(f"Source: {source}")
        if retrieved_at:
            lines.append(f"Retrieved: {retrieved_at}")
        if valid_until:
            lines.append(f"Valid until: {valid_until}")
        if coverage_summary:
            lines.append(f"Coverage: {coverage_summary}")
        if warning_lines:
            lines.append("Warnings:")
            for item in warning_lines:
                lines.append(f"- {item}")
        return "\n".join(lines).strip()

    def _follow_up_actions_allowed(self, detailed_response: str) -> bool:
        lowered = (detailed_response or "").strip().lower()
        if not lowered:
            return False
        blockers = (
            "i do not have",
            "i don't have",
            "not available",
            "unable to",
            "could not",
            "can't",
            "cannot",
            "need more information",
            "please clarify",
        )
        return not any(marker in lowered for marker in blockers)

    def _build_response(self, base_status: IrisStatus, cancel_event: threading.Event | None = None) -> IrisResponse:
        if cancel_event is not None and cancel_event.is_set():
            base_status = IrisStatus.CANCELLED
        requires_confirmation = self.action_executor.has_pending_confirmation()
        status = IrisStatus.AWAITING_CONFIRMATION if requires_confirmation else base_status
        if status not in {IrisStatus.ERROR, IrisStatus.CANCELLED, IrisStatus.AWAITING_CONFIRMATION}:
            status = IrisStatus.COMPLETE
        self._current_status = status
        all_messages = list(self._response_messages)
        messages = list(all_messages)
        if self._active_event_handler is not None and self._streamed_message_counts:
            remaining_counts = dict(self._streamed_message_counts)
            filtered: list[IrisMessage] = []
            for message in messages:
                key = (message.role, message.text)
                used = remaining_counts.get(key, 0)
                if used > 0:
                    remaining_counts[key] = used - 1
                    continue
                filtered.append(message)
            messages = filtered
        response_type = self._response_type_for_status(status)
        if status == IrisStatus.COMPLETE and self._detail_type_override:
            response_type = self._detail_type_override
        topic = self._build_topic_context(status=status, response_type=response_type)
        details = self._build_details_content(all_messages, status=status, response_type=response_type, requires_confirmation=requires_confirmation)
        if (
            status == IrisStatus.COMPLETE
            and response_type in {"text", "markdown"}
            and topic.relationship == "new_topic"
        ):
            details.metadata["render_operation"] = "replace_workspace"
        conversation = self._build_conversation_content_from_details(details, all_messages, response_type=response_type)
        metadata: dict[str, Any] = {}
        if status == IrisStatus.CANCELLED:
            metadata["cancelled"] = True
        if requires_confirmation:
            metadata["awaiting_confirmation"] = True
        return IrisResponse(
            messages=messages,
            status=status,
            response_type=response_type,
            requires_confirmation=requires_confirmation,
            conversation=conversation,
            topic=topic,
            details=details,
            metadata=metadata,
        )

    def _sink(self, text: str, role: str | None) -> None:
        normalized_role = self._normalize_role(role, text)
        self._emit_message(normalized_role, text)

    def _normalize_role(self, role: str | None, text: str) -> MessageRole:
        if role == "progress":
            return MessageRole.PROGRESS
        lowered = text.lower()
        if "requires confirmation" in lowered or "awaiting confirmation" in lowered:
            return MessageRole.CONFIRMATION
        if lowered.startswith("error") or lowered.startswith("assistant could not"):
            return MessageRole.ERROR
        return MessageRole.SYSTEM

    def _emit_message(self, role: MessageRole, text: str, status: IrisStatus | None = None) -> None:
        message = IrisMessage(role=role, text=text)
        self._response_messages.append(message)
        if self._active_event_handler is not None:
            message_status = status or self._current_status
            self._active_event_handler(IrisEvent(status=message_status, message=message))
            key = (role, text)
            self._streamed_message_counts[key] = self._streamed_message_counts.get(key, 0) + 1

    def _emit_event(self, status: IrisStatus) -> None:
        self._current_status = status
        if self._active_event_handler is not None:
            self._active_event_handler(IrisEvent(status=status))

    def _prompt(self, prompt_text: PromptRequest | str) -> str:
        prompt_request = (
            prompt_text
            if isinstance(prompt_text, PromptRequest)
            else PromptRequest(
                prompt_id="legacy-prompt",
                prompt_type=PromptType.TEXT,
                text=prompt_text,
            )
        )
        self._emit_message(MessageRole.CONFIRMATION, prompt_request.text)
        provider = self._active_prompt_provider or self.prompt_provider or input
        if provider is None:
            return ""
        try:
            answer = provider(prompt_request)
        except TypeError:
            answer = provider(prompt_request.text)
        lowered = answer.strip().lower()
        if lowered == PROMPT_CANCEL_TOKEN:
            self._emit_message(MessageRole.USER, "Cancel")
            return answer
        if prompt_request.sensitive:
            self._emit_message(MessageRole.USER, "[hidden]")
            return answer
        display = answer if answer else "(empty)"
        self._emit_message(MessageRole.USER, display)
        return answer

    def _response_type_for_status(self, status: IrisStatus) -> str:
        if status == IrisStatus.INDEXING:
            return "index_summary"
        if status == IrisStatus.SEARCHING:
            return "search_results"
        if status == IrisStatus.AWAITING_CONFIRMATION:
            return "confirmation"
        if status == IrisStatus.CANCELLED:
            return "error"
        return "text"

    def _build_conversation_content_from_details(
        self,
        details: DetailContent,
        messages: list[IrisMessage],
        *,
        response_type: str,
    ) -> ConversationContent:
        if self._conversation_override_message:
            return ConversationContent(message=self._conversation_override_message, suggested_actions=[])

        if details.summary:
            return ConversationContent(
                message=self._summarize_conversation_message(details.summary, response_type=response_type),
                suggested_actions=[],
            )

        return self._build_conversation_content(messages, response_type=response_type)

    def _build_conversation_content(self, messages: list[IrisMessage], *, response_type: str) -> ConversationContent:
        assistant_messages = [message.text for message in messages if message.role == MessageRole.ASSISTANT and message.text.strip()]
        if assistant_messages:
            message = self._summarize_conversation_message("\n".join(assistant_messages).strip(), response_type=response_type)
        else:
            supporting_messages = [
                message.text
                for message in messages
                if message.role in {MessageRole.SYSTEM, MessageRole.PROGRESS, MessageRole.ERROR, MessageRole.CONFIRMATION} and message.text.strip()
            ]
            message = supporting_messages[-1].strip() if supporting_messages else ""
        actions: list[ActionSuggestion] = []
        return ConversationContent(message=message, suggested_actions=actions)

    def _summarize_conversation_message(self, text: str, *, response_type: str) -> str:
        normalized = re.sub(r"\r\n?", "\n", text).strip()
        if not normalized:
            return ""

        if response_type not in {"text", "markdown"} and len(normalized) <= 220 and "\n" not in normalized:
            return normalized

        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", normalized) if paragraph.strip()]
        candidate = paragraphs[0] if paragraphs else normalized
        lines = []
        for line in candidate.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^#{1,6}\s+", stripped):
                continue
            if re.match(r"^[-*•]\s+", stripped):
                continue
            lines.append(stripped)
        candidate = " ".join(lines).strip() if lines else candidate
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if len(candidate) > 220:
            truncated = candidate[:217].rsplit(" ", 1)[0]
            candidate = f"{truncated}..." if truncated else candidate[:220].rstrip() + "..."
        return candidate

    def _build_details_content(
        self,
        messages: list[IrisMessage],
        *,
        status: IrisStatus,
        response_type: str,
        requires_confirmation: bool,
    ) -> DetailContent:
        detail_type = response_type
        title_map = {
            "index_summary": "Indexing summary",
            "search_results": "Search details",
            "confirmation": "Confirmation",
            "error": "Error details",
            "text": "Supporting details",
            "markdown": "Supporting details",
        }
        title = title_map.get(detail_type, "Supporting details")

        canonical = self._details_canonical_topic_state(status=status, response_type=response_type)
        show_topic_state_only = self._is_topic_state_request(self._last_user_message) and canonical is not None
        if show_topic_state_only:
            topic_metadata = dict(canonical.metadata or {})
            topic_metadata["render_operation"] = "replace_workspace"
            return DetailContent(
                type=canonical.type,
                section_id=canonical.section_id,
                title=canonical.title,
                content=canonical.content,
                summary=canonical.summary,
                items=canonical.items,
                actions=canonical.actions,
                metadata=topic_metadata,
            )

        summary = None
        if status == IrisStatus.CANCELLED:
            summary = "The request was cancelled."
        elif requires_confirmation:
            preview = self.action_executor.pending_confirmation_preview()
            summary = preview.summary if preview is not None else "An action is awaiting approval."
        elif messages:
            supporting_texts = [message.text for message in messages if message.role != MessageRole.USER and message.text.strip()]
            if supporting_texts:
                summary = supporting_texts[-1]

        content = self._detail_content_override if self._detail_content_override is not None else summary
        if content is None:
            content = ""

        if requires_confirmation and self._detail_content_override is None:
            preview = self.action_executor.pending_confirmation_preview()
            if preview is not None:
                detail_type = "markdown"
                title = preview.title or title
                content = self._format_confirmation_preview(preview)
                summary = preview.summary

        if self._detail_type_override in {"markdown", "text"}:
            detail_type = self._detail_type_override
        if self._detail_title_override:
            title = self._detail_title_override

        metadata: dict[str, Any] = {
            "render_operation": "replace_section" if detail_type in {"text", "markdown"} else "append",
        }
        if self._detail_metadata_override:
            metadata.update(self._detail_metadata_override)

        items = [
            {"role": message.role.value, "text": message.text}
            for message in messages
            if message.role != MessageRole.USER and message.text.strip()
        ]
        actions = list(self._detail_actions_override) if status == IrisStatus.COMPLETE else []
        self._section_counter += 1
        section_id = self._detail_section_id(detail_type, metadata)
        if canonical is not None and canonical.items:
            metadata["topic_state"] = canonical.items[0]
        if self._active_event_handler is not None:
            metadata["streamed"] = True
        return DetailContent(
            type=detail_type,
            section_id=section_id,
            title=title,
            content=content,
            summary=str(content).strip() if str(content).strip() else summary,
            items=items,
            actions=actions,
            metadata=metadata,
        )

    def _format_confirmation_preview(self, preview: Any) -> str:
        sections: list[str] = []
        sections.append(f"Summary\n{preview.summary}")
        if preview.target:
            sections.append(f"Target\n{preview.target}")
        if preview.after:
            sections.append(f"After\n```json\n{preview.after}\n```")
        if preview.impact:
            sections.append(f"Impact\n{preview.impact}")
        return "\n\n".join(section.strip() for section in sections if section.strip())

    def _detail_section_id(self, detail_type: str, metadata: dict[str, Any]) -> str:
        if detail_type not in {"text", "markdown"}:
            return f"section-{self._section_counter}"

        capability = str(metadata.get("capability", "")).strip().lower()
        if not capability:
            return "assistant-detail"

        variant = str(metadata.get("detail_variant", "")).strip().lower()
        if not variant:
            variant = str(metadata.get(f"{capability}_range", "")).strip().lower()

        tokens = ["capability", capability]
        if variant:
            tokens.append(re.sub(r"[^a-z0-9]+", "-", variant).strip("-"))
        return "-".join(item for item in tokens if item)

    def _is_topic_state_request(self, user_message: str) -> bool:
        lowered = (user_message or "").strip().lower()
        if not lowered:
            return False
        markers = (
            "where did we leave off",
            "current shortlist",
            "summarize everything we decided",
            "remaining options",
            "show the current state",
            "topic state",
        )
        return any(marker in lowered for marker in markers)

    def _details_canonical_topic_state(self, *, status: IrisStatus, response_type: str) -> DetailContent | None:
        if status != IrisStatus.COMPLETE or response_type not in {"text", "markdown"}:
            return None
        turn = self._last_coordinator_turn
        recalled = turn.recalled_context if turn is not None else None
        if not isinstance(recalled, dict):
            return None
        current = recalled.get("current_topic")
        if not isinstance(current, dict):
            return None

        title = str(current.get("title", "")).strip() or "Topic state"
        goal = str(current.get("goal", "")).strip()
        summary = goal or f"Canonical state for {title}"
        items = [
            {
                "topic_id": current.get("topic_id"),
                "topic_type": current.get("topic_type"),
                "version": current.get("version"),
                "goal": goal,
                "requirements": current.get("requirements", []),
                "items": current.get("items", []),
                "decisions": current.get("decisions", []),
                "open_questions": current.get("open_questions", []),
                "notes": current.get("notes", []),
            }
        ]
        metadata: dict[str, Any] = {"render_mode": "canonical_topic_state"}
        if self._active_event_handler is not None:
            metadata["streamed"] = True
        self._section_counter += 1
        return DetailContent(
            type="topic_state",
            section_id=f"section-{self._section_counter}",
            title=title,
            content="",
            summary=summary,
            items=items,
            actions=list(self._detail_actions_override),
            metadata=metadata,
        )

    def _build_topic_context(self, *, status: IrisStatus, response_type: str) -> TopicContext:
        message = self._last_user_message
        turn = self._last_coordinator_turn
        active_topic_id = turn.topic_id if turn is not None else None
        active_topic_title = turn.topic_title if turn is not None else None
        if (
            status == IrisStatus.COMPLETE
            and response_type in {"text", "markdown"}
            and active_topic_id is not None
            and active_topic_title is not None
        ):
            return TopicContext(id=str(active_topic_id), title=str(active_topic_title), relationship="continue")
        if status != IrisStatus.COMPLETE:
            return self._new_topic("System", relationship="new_topic")
        if response_type not in {"text", "markdown"}:
            return self._new_topic(self._topic_title_for_response_type(response_type), relationship="new_topic")

        proposed_title = self._extract_topic_title(message)
        if self._active_topic_id is None or self._active_topic_title is None:
            return self._new_topic(proposed_title, relationship="new_topic")

        if self._is_follow_up_message(message):
            return TopicContext(id=self._active_topic_id, title=self._active_topic_title, relationship="continue")

        if self._is_related_topic(proposed_title, self._active_topic_title):
            return TopicContext(id=self._active_topic_id, title=self._active_topic_title, relationship="related_topic")

        return self._new_topic(proposed_title, relationship="new_topic")

    def _new_topic(self, title: str, *, relationship: str) -> TopicContext:
        self._topic_counter += 1
        self._active_topic_id = f"topic-{self._topic_counter}"
        self._active_topic_title = title
        return TopicContext(id=self._active_topic_id, title=title, relationship=relationship)

    def _topic_title_for_response_type(self, response_type: str) -> str:
        mapping = {
            "index_summary": "Indexing",
            "search_results": "Search",
            "confirmation": "Confirmation",
            "error": "Error",
        }
        return mapping.get(response_type, "Task")

    def _extract_topic_title(self, user_message: str) -> str:
        normalized = re.sub(r"\s+", " ", (user_message or "").strip())
        if not normalized:
            return "General"

        lowered = normalized.lower()
        if lowered.startswith("now tell me about "):
            normalized = normalized[18:].strip()
            lowered = normalized.lower()

        if lowered in {"tell me more", "tell me more about it", "how does that affect humans?", "what about that?"}:
            return self._active_topic_title or "General"

        about_match = re.search(r"\babout\s+(.+)$", normalized, flags=re.IGNORECASE)
        if about_match:
            candidate = about_match.group(1).strip(" .?!")
            if candidate.lower() in {"it", "its", "that", "this", "them", "those"}:
                return self._active_topic_title or "General"
            return self._to_topic_title(candidate)

        explain_match = re.search(r"\b(explain|describe|compare)\b\s+(.+)$", normalized, flags=re.IGNORECASE)
        if explain_match:
            return self._to_topic_title(explain_match.group(2).strip(" .?!"))

        return self._to_topic_title(normalized)

    def _to_topic_title(self, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip()
        if not cleaned:
            return "General"
        words = [word for word in re.split(r"\s+", cleaned) if word]
        clipped = " ".join(words[:4])
        return clipped[:1].upper() + clipped[1:]

    def _is_follow_up_message(self, message: str) -> bool:
        lowered = (message or "").strip().lower()
        if not lowered:
            return False
        if lowered.startswith("now tell me about"):
            return False
        follow_up_prefixes = (
            "tell me more",
            "how does",
            "what about",
            "and ",
            "also ",
            "can you expand",
            "why does",
            "where does",
            "when does",
        )
        if lowered.startswith(follow_up_prefixes):
            return True
        return bool(re.search(r"\b(it|its|that|those|them)\b", lowered))

    def _is_related_topic(self, candidate: str, active: str) -> bool:
        candidate_tokens = self._topic_tokens(candidate)
        active_tokens = self._topic_tokens(active)
        if not candidate_tokens or not active_tokens:
            return False
        overlap = candidate_tokens.intersection(active_tokens)
        return bool(overlap)

    def _topic_tokens(self, value: str) -> set[str]:
        stopwords = {
            "the",
            "a",
            "an",
            "about",
            "tell",
            "me",
            "more",
            "does",
            "that",
            "this",
            "and",
            "or",
            "of",
            "to",
            "on",
            "for",
            "with",
        }
        tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9']+", value) if token}
        return {token for token in tokens if token not in stopwords}
