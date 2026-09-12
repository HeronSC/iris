# File: iris_service.py

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from core.config.loader import ConfigError
from core.host.service import IrisHost
from core.host.windows_service import available, default_config_path, handle_command_line
from core.observability import configure_logging, log_dir_for
from core.server.app import DEFAULT_HOST, DEFAULT_PORT

USAGE = (
    "iris_service.py install|remove|start|stop|restart|debug   (Windows service, needs pywin32)\n"
    "iris_service.py --console [--config PATH] [--host H] [--port N]   (run the host in this window)"
)


def run_console(config_path: Path, host: str, port: int) -> int:
    configure_logging(log_dir_for(None, config_path))
    try:
        service = IrisHost(config_path, http_host=host, http_port=port)
    except ConfigError as error:
        sys.stderr.write(f"Iris host could not start: {error}\n")
        return 1
    stop = threading.Event()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, lambda *_: stop.set())
    sys.stderr.write(f"Iris host running on http://{host}:{port} — Ctrl+C stops it.\n")
    service.run_forever(stop)
    return 0


def main() -> int:
    if "--console" in sys.argv[1:]:
        parser = argparse.ArgumentParser(description="Run the Iris host in the foreground.")
        parser.add_argument("--console", action="store_true")
        parser.add_argument("--config", type=Path, default=default_config_path())
        parser.add_argument("--host", default=DEFAULT_HOST)
        parser.add_argument("--port", type=int, default=DEFAULT_PORT)
        args = parser.parse_args()
        return run_console(args.config, args.host, args.port)
    if not available():
        sys.stderr.write("pywin32 is not installed here; use --console to run the host in the foreground.\n" + USAGE + "\n")
        return 1
    return handle_command_line(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
