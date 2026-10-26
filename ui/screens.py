# File: ui/screens.py

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtGui import QGuiApplication, QScreen

IDENTITY_KEYS = ("name", "manufacturer", "model", "serial")


def screen_identity(screen: QScreen) -> dict[str, str]:
    return {"name": screen.name(), "manufacturer": screen.manufacturer(), "model": screen.model(), "serial": screen.serialNumber()}


def screen_label(screen: QScreen) -> str:
    geometry = screen.geometry()
    parts = [part for part in (screen.model() or screen.name(), screen.manufacturer()) if part]
    primary = " (primary)" if screen is QGuiApplication.primaryScreen() else ""
    return f"{' '.join(parts)} {geometry.width()}x{geometry.height()}{primary}"


def same_screen(identity: dict | None, screen: QScreen) -> bool:
    if not identity:
        return False
    mine = screen_identity(screen)
    if identity.get("serial") and mine["serial"]:
        return identity["serial"] == mine["serial"] and identity.get("model", mine["model"]) == mine["model"]
    return all(identity.get(key, mine[key]) == mine[key] for key in ("name", "model", "manufacturer"))


def find_screen(identity: dict | None) -> QScreen | None:
    for screen in QGuiApplication.screens():
        if same_screen(identity, screen):
            return screen
    return None


def room_screen_setting(config_path: Path) -> dict | None:
    if not config_path.exists():
        return None
    config = json.loads(config_path.read_text(encoding="utf-8"))
    value = config.get("ui", {}).get("room_screen")
    return value if isinstance(value, dict) and any(value.get(key) for key in IDENTITY_KEYS) else None


def save_room_screen(config_path: Path, identity: dict | None) -> None:
    raw = config_path.read_bytes()
    config = json.loads(raw.decode("utf-8"))
    ui = config.setdefault("ui", {})
    if identity:
        ui["room_screen"] = {key: identity.get(key, "") for key in IDENTITY_KEYS}
    else:
        ui.pop("room_screen", None)
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    config_path.write_text(text, encoding="utf-8", newline="\r\n" if b"\r\n" in raw else "\n")


__all__ = ["find_screen", "room_screen_setting", "same_screen", "save_room_screen", "screen_identity", "screen_label"]
