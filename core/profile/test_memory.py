from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).resolve().with_name("loader.py")
spec = importlib.util.spec_from_file_location("memory_loader", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
AssistantMemoryError = module.AssistantMemoryError
MemoryLoader = module.MemoryLoader


class ConfigurationError(Exception):
    pass


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise ConfigurationError(f"Configuration file is missing: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"Invalid JSON in {config_path.name} "
            f"at line {error.lineno}, column {error.colno}: "
            f"{error.msg}"
        ) from error
    except OSError as error:
        raise ConfigurationError(
            f"Could not read {config_path}: {error}"
        ) from error

    if not isinstance(config, dict):
        raise ConfigurationError(
            f"{config_path.name} must contain a JSON object at the top level."
        )

    memory_path = config.get("memory_path")

    if not isinstance(memory_path, str) or not memory_path.strip():
        raise ConfigurationError(
            'config.json must contain a non-empty "memory_path" value.'
        )

    return config


def main() -> None:
    code_folder = Path(__file__).resolve().parent
    config_path = code_folder / "config.json"

    try:
        config = load_config(config_path)
        loader = MemoryLoader(config["memory_path"])
        memory = loader.load()
    except (ConfigurationError, AssistantMemoryError) as error:
        print(f"Assistant could not start: {error}")
        return

    assistant_name = config.get("assistant_name", "Assistant")

    print(f"{assistant_name} memory loaded successfully.")
    print(f"Memory folder: {Path(config['memory_path'])}")
    print(f"Profile sections: {list(memory['profile'].keys())}")
    print(
        f"Preferences: "
        f"{len(memory['preferences'].get('preferences', []))}"
    )
    print(
        f"Projects: "
        f"{len(memory['projects'].get('projects', []))}"
    )
    print(
        f"Knowledge areas: "
        f"{len(memory['knowledge'].get('knowledge_areas', []))}"
    )


if __name__ == "__main__":
    main()
