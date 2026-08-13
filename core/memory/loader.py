from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar


class AssistantMemoryError(Exception):
    pass


class MemoryLoader:
    REQUIRED_FILES: ClassVar[dict[str, str]] = {
        "profile": "profile.json",
        "preferences": "preferences.json",
        "projects": "projects.json",
        "knowledge": "knowledge.json",
    }
    DEFAULT_MEMORY_CONTENT: ClassVar[dict[str, dict[str, Any]]] = {
        "profile": {
            "schema_version": 1,
            "profile": {},
            "metadata": {},
        },
        "preferences": {
            "schema_version": 1,
            "preferences": [],
            "metadata": {},
        },
        "projects": {
            "schema_version": 1,
            "projects": [],
            "metadata": {},
        },
        "knowledge": {
            "schema_version": 1,
            "knowledge_areas": [],
            "documents": [],
            "metadata": {},
        },
    }

    def __init__(self, memory_folder: str | Path) -> None:
        self.memory_folder = Path(memory_folder).expanduser()

    def load(self) -> dict[str, Any]:
        if self.memory_folder.exists() and not self.memory_folder.is_dir():
            raise AssistantMemoryError(
                f"Memory folder is not a directory: {self.memory_folder}"
            )

        self.memory_folder.mkdir(parents=True, exist_ok=True)

        memory: dict[str, Any] = {}

        for key, filename in self.REQUIRED_FILES.items():
            file_path = self.memory_folder / filename
            self._ensure_file_exists(key, file_path)
            memory[key] = self._load_json_file(file_path)

        return memory

    def _ensure_file_exists(self, key: str, file_path: Path) -> None:
        if file_path.exists():
            return

        default_content = self.DEFAULT_MEMORY_CONTENT.get(key)
        if default_content is None:
            raise AssistantMemoryError(f"No default memory content defined for {key}")

        try:
            with file_path.open("w", encoding="utf-8") as file:
                json.dump(default_content, file, indent=2)
                file.write("\n")
        except OSError as error:
            raise AssistantMemoryError(
                f"Could not create required memory file {file_path}: {error}"
            ) from error

    def _load_json_file(self, file_path: Path) -> dict[str, Any]:
        if not file_path.exists():
            raise AssistantMemoryError(
                f"Required memory file is missing: {file_path}"
            )

        try:
            with file_path.open("r", encoding="utf-8-sig") as file:
                data = json.load(file)
        except json.JSONDecodeError as error:
            raise AssistantMemoryError(
                f"Invalid JSON in {file_path.name} "
                f"at line {error.lineno}, column {error.colno}: "
                f"{error.msg}"
            ) from error
        except OSError as error:
            raise AssistantMemoryError(
                f"Could not read {file_path}: {error}"
            ) from error

        if not isinstance(data, dict):
            raise AssistantMemoryError(
                f"{file_path.name} must contain a JSON object at the top level."
            )

        return data
