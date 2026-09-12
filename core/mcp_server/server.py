# File: core/mcp_server/server.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import structlog

from core.mcp_server.service import IrisKnowledgeService
from core.mcp_server.tools import IrisMcpTools
from core.observability import configure_logging, log_dir_for

logger = structlog.get_logger(__name__)

SERVER_NAME = "iris"

INSTRUCTIONS = (
    "Iris's memory and judgement. recall retrieves what Iris knows about something; observe and "
    "close_observation record what happened; hypothesize and add_evidence build a claim the "
    "evidence has to support; assess scores records against accepted rules; review_queue shows what "
    "is waiting on a person. Iris never accepts a hypothesis on its own -- reaching supported is "
    "arithmetic, accepting it is a decision someone makes."
)

READ_ONLY = {"recall", "review_queue", "explain"}


def build_tools(service: IrisKnowledgeService, client: str = "mcp") -> IrisMcpTools:
    return IrisMcpTools(
        service.knowledge,
        service.knowledge_retriever,
        service.knowledge_review,
        permissions=service.permissions,
        auditor=service.tool_auditor,
        client=client,
    )


def build_server(service: IrisKnowledgeService, client: str = "mcp") -> Any:
    #! @allow-local-import
    from mcp.server.mcpserver import MCPServer
    #! @allow-local-import
    from mcp.types import ToolAnnotations

    tools = build_tools(service, client=client)
    server = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS, version="1")

    def register(name: str, handler: Any, description: str) -> None:
        server.add_tool(
            handler,
            name=name,
            description=description,
            annotations=ToolAnnotations(read_only_hint=name in READ_ONLY, destructive_hint=False),
            structured_output=True,
        )

    def recall(text: str, topic: str | None = None, kinds: Sequence[str] | None = None, limit: int = 8) -> dict[str, Any]:
        return tools.recall(text, topic=topic, kinds=kinds, limit=limit)

    def observe(topic: str, content: str, source: str | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return tools.observe(topic, content, source=source, data=data)

    def close_observation(observation_id: str, content: str, favourable: bool | None = None) -> dict[str, Any]:
        return tools.close_observation(observation_id, content, favourable=favourable)

    def hypothesize(topic: str, content: str, source: str | None = None) -> dict[str, Any]:
        return tools.hypothesize(topic, content, source=source)

    def add_evidence(hypothesis_id: str, record_id: str, supports: bool, note: str | None = None) -> dict[str, Any]:
        return tools.add_evidence(hypothesis_id, record_id, supports, note=note)

    def assess(ids: Sequence[str], record: bool = False) -> dict[str, Any]:
        return tools.assess(ids, record=record)

    def review_queue(topic: str | None = None, limit: int = 20) -> dict[str, Any]:
        return tools.review_queue(topic=topic, limit=limit)

    def explain(memory_id: str, max_depth: int = 3) -> dict[str, Any]:
        return tools.explain(memory_id, max_depth=max_depth)

    register("recall", recall, "Retrieve what Iris knows about something: facts, observations, outcomes, decisions.")
    register("observe", observe, "Record something that happened, under a topic, for Iris to learn from later.")
    register("close_observation", close_observation, "Close an observation with what actually happened.")
    register("hypothesize", hypothesize, "Propose a claim for Iris to test against evidence.")
    register("add_evidence", add_evidence, "Attach a record to a hypothesis as evidence for or against it.")
    register("assess", assess, "Score records against the rules Iris has accepted, with the reasoning.")
    register("review_queue", review_queue, "Hypotheses waiting on a person, and those still under test.")
    register("explain", explain, "Where a memory came from and what it rests on.")
    return server


def main(config_path: str | Path | None = None, client: str = "mcp") -> int:
    service = IrisKnowledgeService(config_path)
    configure_logging(log_dir_for(service.config))
    logger.info("mcp server starting", **service.describe())
    build_server(service, client=client).run("stdio")
    return 0


__all__ = ["INSTRUCTIONS", "SERVER_NAME", "build_server", "build_tools", "main"]
