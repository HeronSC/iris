from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any


class MemoryWriter:
    def __init__(self, memory_folder: str | Path) -> None:
        self.memory_folder = Path(memory_folder).expanduser()
        if not self.memory_folder.exists() or not self.memory_folder.is_dir():
            raise FileNotFoundError(
                f"Memory folder does not exist: {self.memory_folder}"
            )

    def save_profile(self, data: dict[str, Any]) -> None:
        self._write_json_file("profile.json", data)

    def save_preferences(self, data: dict[str, Any]) -> None:
        self._write_json_file("preferences.json", data)

    def save_projects(self, data: dict[str, Any]) -> None:
        self._write_json_file("projects.json", data)

    def save_knowledge(self, data: dict[str, Any]) -> None:
        self._write_json_file("knowledge.json", data)

    def _write_json_file(self, filename: str, data: dict[str, Any]) -> None:
        file_path = self.memory_folder / filename
        if file_path.exists():
            backup_path = file_path.with_suffix(file_path.suffix + ".bak")
            shutil.copy2(file_path, backup_path)

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.memory_folder, delete=False) as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                temp_path = Path(handle.name)
            if temp_path is not None:
                temp_path.replace(file_path)
        finally:
            if temp_path is not None and temp_path.exists() and temp_path != file_path:
                temp_path.unlink(missing_ok=True)
