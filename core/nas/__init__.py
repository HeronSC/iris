# File: core/nas/__init__.py

from __future__ import annotations

import logging
from typing import Any

from core.nas.synology import (
    PASSWORD_SECRET,
    Disk,
    Share,
    SynologyClient,
    SynologyError,
    SynologyPermissionError,
    SystemInfo,
    Utilization,
    Volume,
)

logger = logging.getLogger(__name__)

NOT_CONFIGURED = "The NAS is not configured: set synology.url and synology.user in config.json and the password with /secrets set synology:password <value>."


class NasService:
    def __init__(self, client: SynologyClient | None, *, configured: bool = True, problem: str | None = None) -> None:
        self.client = client
        self.configured = configured and client is not None
        self.problem = problem

    @classmethod
    def from_config(cls, config: dict[str, Any], secrets: Any = None, *, transport: Any = None) -> "NasService":
        section = config.get("synology") if isinstance(config.get("synology"), dict) else {}
        url = str(section.get("url") or "").strip()
        user = str(section.get("user") or "").strip()
        if not url or not user:
            return cls(None, configured=False, problem=NOT_CONFIGURED)
        secret_name = str(section.get("password_secret") or PASSWORD_SECRET)
        password = ""
        if secrets is not None:
            try:
                password = secrets.get(secret_name) or ""
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                logger.warning("Could not read %s: %s", secret_name, error)
        if not password:
            return cls(None, configured=False, problem=f"The NAS password is not set: /secrets set {secret_name} <value>")
        return cls(
            SynologyClient(
                url,
                user,
                password,
                timeout_seconds=float(section.get("timeout_seconds", 15.0)),
                verify=bool(section.get("verify_certificate", False)),
                transport=transport,
            )
        )

    def _require(self) -> SynologyClient:
        if self.client is None:
            raise SynologyError(self.problem or NOT_CONFIGURED)
        return self.client

    def system_info(self) -> SystemInfo:
        return self._require().system_info()

    def utilization(self) -> Utilization:
        return self._require().utilization()

    def storage(self) -> tuple[list[Volume], list[Disk]]:
        return self._require().storage()

    def shares(self) -> list[Share]:
        return self._require().shares()

    def hostname(self) -> str:
        return self._require().hostname()

    def unhealthy(self) -> tuple[list[Volume], list[Disk]]:
        volumes, disks = self.storage()
        return [volume for volume in volumes if not volume.healthy], [disk for disk in disks if not disk.healthy]


__all__ = ["NasService", "NOT_CONFIGURED", "SynologyError", "SynologyPermissionError"]
