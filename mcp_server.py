# File: mcp_server.py

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.config.loader import ConfigError
from core.mcp_server.server import main as serve
from core.profile.loader import AssistantMemoryError

root = Path(__file__).resolve().parent

DEFAULT_CONFIG = root / "core" / "config.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Iris's memory to an MCP client over stdio.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--client", default="mcp", help="Who is calling, for the audit trail")
    args = parser.parse_args()

    try:
        return serve(args.config, client=args.client)
    except (ConfigError, AssistantMemoryError) as error:
        sys.stderr.write(f"Iris could not start: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
