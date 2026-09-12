# File: core/permissions/models.py

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.tools.models import PermissionLevel


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionRequest:
    tool: str
    permission: PermissionLevel = PermissionLevel.READ
    action: str | None = None
    source: str = "command"
    target: str | None = None
    paths: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    outbound: bool = False

    @property
    def subject(self) -> str:
        return self.action or self.tool


@dataclass(frozen=True)
class PermissionDecision:
    decision: Decision
    reason: str
    rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is not Decision.DENY

    @property
    def denied(self) -> bool:
        return self.decision is Decision.DENY

    @property
    def requires_confirmation(self) -> bool:
        return self.decision is Decision.CONFIRM


class PermissionDenied(Exception):
    def __init__(self, decision: PermissionDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


__all__ = ["Decision", "PermissionDecision", "PermissionDenied", "PermissionLevel", "PermissionRequest"]
