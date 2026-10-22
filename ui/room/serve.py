# File: ui/room/serve.py

import functools
import http.server
import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path

from ui.room.manifest import build_manifest

ROOM = Path(__file__).resolve().parent
ROOT = ROOM.parents[1]
DEFAULT_ASSETS = "E:/Reallusion/Iris"
log = logging.getLogger("iris.room")


def asset_root() -> Path:
    config_path = Path(os.getenv("IRIS_CONFIG_PATH", "").strip() or ROOT / "core" / "config.json")
    configured = ""
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        configured = str(config.get("ui", {}).get("room", {}).get("assets", "") or "")
    return Path(configured or DEFAULT_ASSETS)


class RoomHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, assets: Path, **kwargs):
        self.assets = assets
        super().__init__(*args, directory=str(ROOM), **kwargs)

    def do_GET(self) -> None:
        clean = self.path.split("?", 1)[0].split("#", 1)[0]
        if clean == "/assets/manifest.json":
            body = json.dumps(build_manifest(self.assets)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if not clean.startswith("/assets/"):
            return super().translate_path(path)
        relative = urllib.parse.unquote(clean[len("/assets/"):])
        target = (self.assets / relative).resolve()
        if not target.is_relative_to(self.assets.resolve()):
            return str(self.assets / "forbidden")
        if clean.endswith("/"):
            return str(target) + os.sep
        return str(target)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    port = int(argv[0]) if argv else 8790
    assets = asset_root()
    handler = functools.partial(RoomHandler, assets=assets)
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        log.info("Serving %s at http://127.0.0.1:%s/ with assets from %s (close this window to stop)", ROOM, port, assets)
        server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
