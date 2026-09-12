# File: core/permissions/policy.py

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from core.audit.stream import AuditCategory, AuditEvent, AuditStream
from core.permissions.limits import RateLimit, RateLimiter
from core.permissions.models import Decision, PermissionDecision, PermissionLevel, PermissionRequest
from core.permissions.targets import host_matches, within_any

logger = logging.getLogger(__name__)

DEFAULT_MODES: dict[PermissionLevel, Decision] = {
    PermissionLevel.READ: Decision.ALLOW,
    PermissionLevel.WRITE: Decision.ALLOW,
    PermissionLevel.EXECUTE: Decision.ALLOW,
}

OUTBOUND_BUCKET = "outbound"

DEFAULT_LIMITS: dict[str, RateLimit] = {OUTBOUND_BUCKET: RateLimit(limit=120, per_seconds=3600.0)}

ALLOWED = PermissionDecision(decision=Decision.ALLOW, reason="Allowed", rule="default")


class PermissionPolicy:
    def __init__(
        self,
        *,
        modes: dict[PermissionLevel, Decision] | None = None,
        allowed_paths: Iterable[Path | str] = (),
        denied_paths: Iterable[Path | str] = (),
        allowed_hosts: Iterable[str] = (),
        denied_hosts: Iterable[str] = (),
        limiter: RateLimiter | None = None,
        audit: AuditStream | None = None,
    ) -> None:
        self.modes = {**DEFAULT_MODES, **(modes or {})}
        self.allowed_paths = [Path(str(item)).expanduser() for item in allowed_paths]
        self.denied_paths = [Path(str(item)).expanduser() for item in denied_paths]
        self.allowed_hosts = tuple(str(item).strip().lower() for item in allowed_hosts if str(item).strip())
        self.denied_hosts = tuple(str(item).strip().lower() for item in denied_hosts if str(item).strip())
        self.limiter = limiter or RateLimiter(DEFAULT_LIMITS)
        self.audit = audit
        self.halted = False
        self.halted_reason = ""

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        default_allowed_paths: Iterable[Path | str] = (),
        audit: AuditStream | None = None,
    ) -> "PermissionPolicy":
        settings = config if isinstance(config, dict) else {}
        modes: dict[PermissionLevel, Decision] = {}
        for level in PermissionLevel:
            raw = settings.get(level.value)
            if raw is None:
                continue
            try:
                modes[level] = Decision(str(raw).strip().lower())
            except ValueError:
                logger.warning("Unknown permission mode for %s: %r", level.value, raw)
        allowed_paths = settings.get("allowed_paths")
        limits = {
            name: RateLimit.from_json(payload)
            for name, payload in (settings.get("rate_limits") or {}).items()
        }
        return cls(
            modes=modes,
            allowed_paths=allowed_paths if allowed_paths else list(default_allowed_paths),
            denied_paths=settings.get("denied_paths") or (),
            allowed_hosts=settings.get("allowed_hosts") or (),
            denied_hosts=settings.get("denied_hosts") or (),
            limiter=RateLimiter({**DEFAULT_LIMITS, **limits}),
            audit=audit,
        )

    def halt(self, reason: str = "Iris is stopped") -> None:
        self.halted = True
        self.halted_reason = reason

    def release(self) -> None:
        self.halted = False
        self.halted_reason = ""

    def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        if self.halted:
            return PermissionDecision(Decision.DENY, f"{self.halted_reason or 'Iris is stopped'}; /resume to continue", "halted")
        for path in request.paths:
            if self.denied_paths and within_any(path, self.denied_paths):
                return PermissionDecision(Decision.DENY, f"{path} is on the denied list", "denied_paths")
            if self.allowed_paths and not within_any(path, self.allowed_paths):
                return PermissionDecision(
                    Decision.DENY,
                    f"{path} is outside the folders Iris may touch",
                    "allowed_paths",
                )
        for host in request.hosts:
            if self.denied_hosts and host_matches(host, self.denied_hosts):
                return PermissionDecision(Decision.DENY, f"{host} is on the denied list", "denied_hosts")
            if self.allowed_hosts and not host_matches(host, self.allowed_hosts):
                return PermissionDecision(Decision.DENY, f"{host} is not an allowed host", "allowed_hosts")
        mode = self.modes.get(request.permission, Decision.CONFIRM)
        if mode is Decision.DENY:
            return PermissionDecision(Decision.DENY, f"{request.permission.value} is turned off", "level")
        if mode is Decision.CONFIRM:
            return PermissionDecision(
                Decision.CONFIRM,
                f"{request.subject} needs {request.permission.value} access",
                "level",
            )
        return ALLOWED

    def enforce(self, request: PermissionRequest) -> PermissionDecision:
        decision = self.evaluate(request)
        if decision.allowed and request.outbound and not self.limiter.consume(OUTBOUND_BUCKET):
            limit = self.limiter.limit_for(OUTBOUND_BUCKET)
            decision = PermissionDecision(
                Decision.DENY,
                f"Outbound cap reached ({limit.describe() if limit else 'capped'})",
                "rate_limit",
            )
        if decision.denied:
            self._record(request, decision)
        return decision

    def outbound_remaining(self) -> int | None:
        return self.limiter.remaining(OUTBOUND_BUCKET)

    def describe(self) -> dict[str, Any]:
        return {
            "modes": {level.value: self.modes[level].value for level in PermissionLevel},
            "allowed_paths": [str(item) for item in self.allowed_paths],
            "denied_paths": [str(item) for item in self.denied_paths],
            "allowed_hosts": list(self.allowed_hosts),
            "denied_hosts": list(self.denied_hosts),
            "rate_limits": {name: limit.describe() for name, limit in self.limiter.limits.items()},
            "outbound_remaining": self.outbound_remaining(),
            "halted": self.halted,
        }

    def _record(self, request: PermissionRequest, decision: PermissionDecision) -> None:
        if self.audit is None:
            return
        try:
            self.audit.write(
                AuditEvent(
                    category=AuditCategory.PERMISSION,
                    event=request.subject,
                    subject=request.permission.value,
                    target=request.target,
                    source=request.source,
                    status="denied",
                    message=decision.reason,
                    data={"rule": decision.rule, "paths": list(request.paths), "hosts": list(request.hosts)},
                )
            )
        except Exception as error:
            logger.warning("Permission audit failed for %s: %s", request.subject, error)


__all__ = ["DEFAULT_LIMITS", "DEFAULT_MODES", "OUTBOUND_BUCKET", "PermissionPolicy"]
