# File: ui/room/manifest.py

from pathlib import Path

ASSET_SUFFIXES = {".glb"}


def build_manifest(root: Path) -> dict:
    files: list[str] = []
    for path in root.rglob("*"):
        if path.suffix.lower() not in ASSET_SUFFIXES or not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return {"files": sorted(files, key=str.lower)}
