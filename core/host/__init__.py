# File: core/host/__init__.py

from __future__ import annotations

from core.host.health import HOST_DESKTOP, HOST_MCP, HOST_SERVICE, health_report
from core.host.knowledge import IrisKnowledgeService
from core.host.service import IrisHost, probe_host

__all__ = [
    "HOST_DESKTOP",
    "HOST_MCP",
    "HOST_SERVICE",
    "IrisHost",
    "IrisKnowledgeService",
    "health_report",
    "probe_host",
]
