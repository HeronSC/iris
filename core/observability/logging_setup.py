# File: core/observability/logging_setup.py

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 5
LOG_FILE_NAME = "iris.jsonl"

_CONFIGURED_MARKER = "_iris_logging_handler"


def log_dir_for(config: dict[str, Any] | None, config_path: str | Path | None = None) -> Path:
    if config is None and config_path is not None:
        try:
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
    config = config or {}
    explicit = str(config.get("log_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    memory_path = str(config.get("memory_path") or "").strip()
    if memory_path:
        return Path(memory_path).expanduser().parent / "logs"
    return Path.cwd() / "logs"


def configure_logging(
    log_dir: str | Path,
    *,
    level: int | str = logging.INFO,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backups: int = DEFAULT_BACKUPS,
    console_level: int | str = logging.WARNING,
) -> Path:
    directory = Path(log_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / LOG_FILE_NAME

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _CONFIGURED_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    json_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=int(max_bytes), backupCount=int(backups), encoding="utf-8"
    )
    file_handler.setFormatter(json_formatter)
    file_handler.setLevel(logging.DEBUG)
    setattr(file_handler, _CONFIGURED_MARKER, True)

    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.KeyValueRenderer(key_order=["timestamp", "level", "logger", "event", "request_id"]),
        ],
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(console_level)
    setattr(console_handler, _CONFIGURED_MARKER, True)

    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(level)
    return log_file


def read_log_entries(log_file: str | Path, *, request_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    path = Path(log_file)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if request_id is not None and entry.get("request_id") != request_id:
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    entries.reverse()
    return entries
