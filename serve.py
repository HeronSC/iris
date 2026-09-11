# File: serve.py

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from core.application import IrisApplication
from core.config.loader import ConfigError
from core.observability import configure_logging, log_dir_for
from core.profile.loader import AssistantMemoryError
from core.server.app import DEFAULT_HOST, DEFAULT_PORT, create_app

root = Path(__file__).resolve().parent


DEFAULT_CONFIG = root / "core" / "config.json"


def build(config_path: Path | None = None) -> tuple[object, IrisApplication]:
    configure_logging(log_dir_for(None, config_path or DEFAULT_CONFIG))
    app_service = IrisApplication(config_path or DEFAULT_CONFIG)
    app_service.initialize()
    return create_app(app_service), app_service


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Iris over HTTP on localhost.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    try:
        api, app_service = build(args.config)
    except (ConfigError, AssistantMemoryError) as error:
        sys.stderr.write(f"Iris could not start: {error}\n")
        return 1

    try:
        uvicorn.run(api, host=args.host, port=args.port, log_level=args.log_level)
    finally:
        app_service.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
