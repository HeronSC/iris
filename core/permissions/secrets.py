# File: core/permissions/secrets.py

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

DEFAULT_SERVICE = "Iris"

ENVIRONMENT_PREFIX = "IRIS_SECRET_"


class SecretError(Exception):
    pass


def environment_name(name: str) -> str:
    return ENVIRONMENT_PREFIX + "".join(character if character.isalnum() else "_" for character in name).upper()


class SecretStore:
    def __init__(
        self,
        service: str = DEFAULT_SERVICE,
        *,
        index_path: str | Path | None = None,
        backend: Any | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.service = service
        self.index_path = Path(index_path) if index_path else None
        self.environ = environ if environ is not None else os.environ
        self._backend = backend
        self._backend_loaded = backend is not None
        self._lock = threading.Lock()

    @property
    def backend(self) -> Any | None:
        if not self._backend_loaded:
            self._backend_loaded = True
            self._backend = self._load_backend()
        return self._backend

    @property
    def available(self) -> bool:
        return self.backend is not None

    def get(self, name: str) -> str | None:
        backend = self.backend
        if backend is not None:
            try:
                value = backend.get_password(self.service, name)
            except Exception as error:
                logger.warning("Credential store unavailable for %s: %s", name, error)
                value = None
            if value:
                return str(value)
        return self.environ.get(environment_name(name)) or None

    def set(self, name: str, value: str) -> None:
        if not str(name).strip():
            raise SecretError("A secret needs a name")
        if not str(value):
            raise SecretError("A secret needs a value")
        backend = self.backend
        if backend is None:
            raise SecretError(
                "No credential store on this machine. Set "
                f"{environment_name(name)} in the environment instead."
            )
        backend.set_password(self.service, name, value)
        self._remember(name)

    def delete(self, name: str) -> bool:
        backend = self.backend
        removed = False
        if backend is not None:
            try:
                backend.delete_password(self.service, name)
                removed = True
            except Exception as error:
                logger.info("Nothing to delete for %s: %s", name, error)
        self._forget(name)
        return removed

    def names(self) -> list[str]:
        return sorted(self._index())

    def describe(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in self.names():
            in_environment = environment_name(name) in self.environ
            rows.append(
                {
                    "name": name,
                    "set": self.get(name) is not None,
                    "source": "environment" if in_environment and not self.available else "credential store",
                }
            )
        return rows

    def _load_backend(self) -> Any | None:
        try:
            #! @allow-local-import
            import keyring
        except ImportError:
            logger.info("keyring is not installed; secrets come from the environment only")
            return None
        try:
            backend = keyring.get_keyring()
        except Exception as error:
            logger.warning("No usable credential store: %s", error)
            return None
        name = type(backend).__name__.lower()
        if "fail" in name or "null" in name:
            logger.info("No credential store on this machine (%s)", type(backend).__name__)
            return None
        return keyring

    def _index(self) -> set[str]:
        if self.index_path is None or not self.index_path.exists():
            return set()
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        names = payload.get("secrets") if isinstance(payload, dict) else payload
        return {str(item) for item in names} if isinstance(names, list) else set()

    def _remember(self, name: str) -> None:
        if self.index_path is None:
            return
        with self._lock:
            names = self._index() | {name}
            self._write_index(names)

    def _forget(self, name: str) -> None:
        if self.index_path is None:
            return
        with self._lock:
            names = self._index() - {name}
            self._write_index(names)

    def _write_index(self, names: set[str]) -> None:
        if self.index_path is None:
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"secrets": sorted(names)}
        self.index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


__all__ = ["DEFAULT_SERVICE", "ENVIRONMENT_PREFIX", "SecretError", "SecretStore", "environment_name"]
