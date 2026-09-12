# File: core/mcp_server/server.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import structlog

from core.actions.bootstrap import documents_roots_for
from core.code import CodeService
from core.host.knowledge import IrisKnowledgeService
from core.mcp_server.code_tools import IrisCodeTools
from core.mcp_server.tools import IrisMcpTools
from core.observability import configure_logging, log_dir_for

logger = structlog.get_logger(__name__)

SERVER_NAME = "iris"

INSTRUCTIONS = (
    "Iris's memory and judgement. recall retrieves what Iris knows about something; observe and "
    "close_observation record what happened; hypothesize and add_evidence build a claim the "
    "evidence has to support; assess scores records against accepted rules; review_queue shows what "
    "is waiting on a person. Iris never accepts a hypothesis on its own -- reaching supported is "
    "arithmetic, accepting it is a decision someone makes. For Business Central AL work: workspaces "
    "lists the AL projects Iris may read, describe_workspace reads app.json and the packages, "
    "find_symbol looks up an object with its fields, events and subscribers, search_code greps the "
    "sources, read_file shows a numbered window, compile_workspace runs alc.exe with the workspace's "
    "analyzers and returns every diagnostic."
)

READ_ONLY = {"recall", "review_queue", "explain", "workspaces", "describe_workspace", "find_symbol", "search_code", "read_file"}


def build_tools(service: IrisKnowledgeService, client: str = "mcp") -> IrisMcpTools:
    return IrisMcpTools(
        service.knowledge,
        service.knowledge_retriever,
        service.knowledge_review,
        permissions=service.permissions,
        auditor=service.tool_auditor,
        client=client,
    )


def build_code_tools(service: IrisKnowledgeService, client: str = "mcp", code_service: CodeService | None = None) -> IrisCodeTools:
    config = getattr(service, "config", None)
    config = config if isinstance(config, dict) else {}
    roots = documents_roots_for(config) if config else []
    code_cfg = config.get("code", {}) if isinstance(config.get("code"), dict) else {}
    data_root = getattr(service, "data_root", None)
    return IrisCodeTools(
        code_service or CodeService(roots, cache_dir=(Path(data_root) / "Index" / "al_symbols") if data_root else None, alc_path=code_cfg.get("alc_path") or None),
        allowed_roots=roots,
        auditor=getattr(service, "tool_auditor", None),
        client=client,
    )


def build_server(service: IrisKnowledgeService, client: str = "mcp", code_service: CodeService | None = None) -> Any:
    #! @allow-local-import
    from mcp.server.mcpserver import MCPServer
    #! @allow-local-import
    from mcp.types import ToolAnnotations

    tools = build_tools(service, client=client)
    code_tools = build_code_tools(service, client=client, code_service=code_service)
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

    def workspaces() -> dict[str, Any]:
        return code_tools.workspaces()

    def describe_workspace(path: str | None = None) -> dict[str, Any]:
        return code_tools.describe_workspace(path)

    def find_symbol(name: str, kind: str | None = None, detail: str = "summary", path: str | None = None) -> dict[str, Any]:
        return code_tools.find_symbol(name, kind=kind, detail=detail, path=path)

    def search_code(pattern: str, path: str | None = None, glob: str | None = None, regex: bool = False, max_results: int = 40) -> dict[str, Any]:
        return code_tools.search_code(pattern, path=path, glob=glob, regex=regex, max_results=max_results)

    def read_file(path: str, start_line: int = 1, max_lines: int = 200) -> dict[str, Any]:
        return code_tools.read_file(path, start_line=start_line, max_lines=max_lines)

    def compile_workspace(path: str | None = None, analyzers: bool = True, max_diagnostics: int = 40) -> dict[str, Any]:
        return code_tools.compile_workspace(path, analyzers=analyzers, max_diagnostics=max_diagnostics)

    register("workspaces", workspaces, "The Business Central AL workspaces Iris may read: name, publisher, version, root.")
    register("describe_workspace", describe_workspace, "An AL workspace's app.json, launch targets, symbol packages and object counts; the one at or above a path, or by name.")
    register("find_symbol", find_symbol, "Look up an AL object by name, id or prefix: fields, procedures, events, subscribers in the workspace, extensions targeting it. detail is summary, fields, events, procedures or all.")
    register("search_code", search_code, "Search a workspace's sources with ripgrep: file, line and the matching line.")
    register("read_file", read_file, "A numbered window of a text file inside the folders Iris may read.")
    register("compile_workspace", compile_workspace, "Compile an AL workspace with alc.exe and its analyzers; every error and warning with file, line and code. The .app goes to Iris's build folder.")
    return server


def main(config_path: str | Path | None = None, client: str = "mcp") -> int:
    service = IrisKnowledgeService(config_path)
    configure_logging(log_dir_for(service.config))
    logger.info("mcp server starting", **service.describe())
    build_server(service, client=client).run("stdio")
    return 0


__all__ = ["INSTRUCTIONS", "SERVER_NAME", "build_code_tools", "build_server", "build_tools", "main"]
