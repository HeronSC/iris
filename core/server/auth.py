# File: core/server/auth.py

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from typing import Any

from core.audit.stream import AuditCategory, AuditEvent, AuditStream
from core.permissions.secrets import SecretStore

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-Iris-Token"

SECRET_PREFIX = "api_token:"

DEFAULT_ANONYMOUS_PATHS = ("/health",)

DEFAULT_CLIENTS = ("desktop", "bot")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient", ""})


@dataclass(frozen=True)
class AuthSettings:
    required: bool | None = None
    loopback_only: bool = True
    clients: tuple[str, ...] = DEFAULT_CLIENTS
    anonymous_paths: tuple[str, ...] = DEFAULT_ANONYMOUS_PATHS

    @classmethod
    def from_config(cls, payload: Any) -> "AuthSettings":
        settings = payload if isinstance(payload, dict) else {}
        raw_required = settings.get("require_token")
        clients = settings.get("clients")
        anonymous = settings.get("anonymous_paths")
        return cls(
            required=None if raw_required is None else bool(raw_required),
            loopback_only=bool(settings.get("loopback_only", True)),
            clients=tuple(str(item) for item in clients) if isinstance(clients, list) and clients else DEFAULT_CLIENTS,
            anonymous_paths=tuple(str(item) for item in anonymous)
            if isinstance(anonymous, list)
            else DEFAULT_ANONYMOUS_PATHS,
        )


@dataclass(frozen=True)
class AuthOutcome:
    ok: bool
    client: str = "anonymous"
    status_code: int = 200
    reason: str = ""
    audited: bool = field(default=False, compare=False)


class ApiAuthenticator:
    def __init__(
        self,
        secrets: SecretStore | None = None,
        settings: AuthSettings | None = None,
        audit: AuditStream | None = None,
    ) -> None:
        self.secrets = secrets
        self.settings = settings or AuthSettings()
        self.audit = audit

    def tokens(self) -> dict[str, str]:
        if self.secrets is None:
            return {}
        found: dict[str, str] = {}
        for client in self.settings.clients:
            value = self.secrets.get(f"{SECRET_PREFIX}{client}")
            if value:
                found[client] = value
        return found

    @property
    def enforcing(self) -> bool:
        if self.settings.required is not None:
            return self.settings.required
        return bool(self.tokens())

    def startup_notice(self) -> str | None:
        if self.enforcing:
            return None
        return (
            "The HTTP surface has no token set, so anything on this machine can call it. "
            f"Set one with /secrets set {SECRET_PREFIX}<client> <value>."
        )

    def authenticate(self, *, path: str, token: str | None, client_host: str | None) -> AuthOutcome:
        if self.settings.loopback_only and not is_loopback(client_host):
            return self._refuse(path, client_host, 403, "Iris answers only on this machine")
        if path in self.settings.anonymous_paths or not self.enforcing:
            return AuthOutcome(ok=True)
        known = self.tokens()
        if not known:
            return self._refuse(path, client_host, 503, "No client token is configured on this machine")
        presented = (token or "").strip()
        if not presented:
            return self._refuse(path, client_host, 401, f"{TOKEN_HEADER} is required")
        for client, secret in known.items():
            if hmac.compare_digest(presented, secret):
                return AuthOutcome(ok=True, client=client)
        return self._refuse(path, client_host, 401, "That token is not one of mine")

    def _refuse(self, path: str, client_host: str | None, status_code: int, reason: str) -> AuthOutcome:
        if self.audit is not None:
            try:
                self.audit.write(
                    AuditEvent(
                        category=AuditCategory.PERMISSION,
                        event="http",
                        subject=path,
                        target=client_host,
                        source="http",
                        status="denied",
                        message=reason,
                        data={"status_code": status_code},
                    )
                )
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Could not record a refused request: %s", error)
        return AuthOutcome(ok=False, status_code=status_code, reason=reason, audited=True)


def is_loopback(client_host: str | None) -> bool:
    return (client_host or "").strip().lower() in LOOPBACK_HOSTS


def token_from_headers(headers: Any) -> str | None:
    direct = headers.get(TOKEN_HEADER)
    if direct:
        return str(direct)
    authorization = str(headers.get("authorization") or "")
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix):].strip()
    return None


__all__ = [
    "DEFAULT_ANONYMOUS_PATHS",
    "SECRET_PREFIX",
    "TOKEN_HEADER",
    "ApiAuthenticator",
    "AuthOutcome",
    "AuthSettings",
    "is_loopback",
    "token_from_headers",
]
