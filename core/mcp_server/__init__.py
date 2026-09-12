# File: core/mcp_server/__init__.py

from __future__ import annotations

from core.mcp_server.server import SERVER_NAME, build_server, build_tools
from core.host.knowledge import IrisKnowledgeService
from core.mcp_server.tools import IrisMcpTools, McpToolError

__all__ = [
    "SERVER_NAME",
    "IrisKnowledgeService",
    "IrisMcpTools",
    "McpToolError",
    "build_server",
    "build_tools",
]
