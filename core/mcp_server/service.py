# File: core/mcp_server/service.py

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.audit.stream import AuditStream
from core.config.loader import ConfigLoader
from core.conversation.persistent_memory.models import MemoryConfig
from core.knowledge.embeddings import EmbeddingConfig, MemoryEmbeddingIndex
from core.knowledge.graph import KnowledgeGraph
from core.knowledge.hypotheses import HypothesisTracker
from core.knowledge.retrieval import KnowledgeRetriever
from core.knowledge.review import KnowledgeReviewWorkflow
from core.llm.ollama_client import OllamaClient
from core.llm.router import ModelRouter, ModelRoutes
from core.permissions.policy import PermissionPolicy
from core.permissions.secrets import SecretStore
from core.storage.sqlite_database import SQLiteDatabase
from core.tools.audit import ToolAuditor

logger = logging.getLogger(__name__)


class IrisKnowledgeService:
    def __init__(self, config_path: str | Path | None = None) -> None:
        loader = ConfigLoader(config_path)
        self.config = loader.load()
        self.config_path = loader.config_path

        memory_config = MemoryConfig.from_config(self.config)
        knowledge_path = self.config.get("knowledge_path") or memory_config.database_path.parent / "knowledge.db"
        database = SQLiteDatabase(knowledge_path)
        self.knowledge = KnowledgeGraph(database)
        self.hypotheses = HypothesisTracker(self.knowledge)
        self.model_router = ModelRouter(
            OllamaClient(
                self.config["llm_server"],
                self.config["model"],
                timeout_seconds=self.config.get("llm_timeout_seconds", 30.0),
            ),
            ModelRoutes.from_config(self.config),
        )
        self.embedding_index = MemoryEmbeddingIndex(
            database, self.model_router, EmbeddingConfig.from_config(self.config)
        )
        self.knowledge_retriever = KnowledgeRetriever(database, embeddings=self.embedding_index)

        audit_folder = Path(self.config.get("audit_path") or Path(self.config["memory_path"]).parent / "Audit")
        self.audit_stream = AuditStream(audit_folder)
        self.knowledge_review = KnowledgeReviewWorkflow(self.hypotheses)
        self.secrets = SecretStore(
            index_path=Path(self.config["memory_path"]).parent / "Configuration" / "secrets.json"
        )
        self.permissions = PermissionPolicy.from_config(self.config.get("permissions"), audit=self.audit_stream)
        self.tool_auditor = ToolAuditor(self.audit_stream)

    def describe(self) -> dict[str, Any]:
        return {
            "assistant": str(self.config.get("assistant_name", "Iris")),
            "records": self.knowledge.records.count(),
            "embeddings": self.embedding_index.available,
        }


__all__ = ["IrisKnowledgeService"]
