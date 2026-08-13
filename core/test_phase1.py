import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config.loader import ConfigError, ConfigLoader
from core.conversation.context_builder import ContextBuilder
from core.memory.store import MemoryStore


class PhaseOneTests(unittest.TestCase):
    def test_config_loader_validates_required_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "assistant_name": "Iris",
                        "memory_path": str(Path(tmpdir) / "memory"),
                        "model": "qwen3:30b-a3b",
                        "llm_server": "http://localhost:11434",
                    }
                ),
                encoding="utf-8",
            )
            memory_dir = Path(tmpdir) / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)

            loader = ConfigLoader(config_path)
            config = loader.load()

            self.assertEqual(config["assistant_name"], "Iris")
            self.assertEqual(config["model"], "qwen3:30b-a3b")
            self.assertTrue(isinstance(config["memory_path"], Path))

    def test_context_builder_includes_profile_and_preferences(self) -> None:
        memory_data = {
            "profile": {
                "profile": {
                    "display_name": "Henry",
                    "professional": {
                        "primary_role": "Developer",
                        "primary_language": "Python",
                    },
                    "technical_environment": {
                        "primary_os": "Windows",
                    },
                }
            },
            "preferences": {
                "preferences": [
                    {
                        "id": "coding.minimal_changes",
                        "name": "Prefer minimal changes",
                        "value": True,
                        "status": "active",
                    },
                    {
                        "id": "coding.comments",
                        "name": "Add comments",
                        "value": False,
                        "status": "inactive",
                    },
                ]
            },
            "projects": {"projects": []},
            "knowledge": {"knowledge_areas": []},
        }

        store = MemoryStore(memory_data)
        builder = ContextBuilder("Iris", store)
        context = builder.build_context("How should I write code?", project_id=None)

        self.assertIn("Assistant identity", context)
        self.assertIn("Henry", context)
        self.assertIn("Developer", context)
        self.assertIn("Prefer minimal changes", context)
        self.assertNotIn("Add comments", context)

    def test_memory_store_returns_active_projects(self) -> None:
        memory_data = {
            "profile": {},
            "preferences": {"preferences": []},
            "projects": {
                "projects": [
                    {"id": "iris", "name": "Iris", "status": "active"},
                    {"id": "mammoth", "name": "Mammoth", "status": "inactive"},
                ]
            },
            "knowledge": {"knowledge_areas": []},
        }

        store = MemoryStore(memory_data)

        self.assertEqual(len(store.get_active_projects()), 1)
        self.assertEqual(store.get_project("iris")["name"], "Iris")
        self.assertEqual(store.get_project("IRIS")["name"], "Iris")

    def test_config_loader_rejects_missing_required_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps({"assistant_name": "Iris"}), encoding="utf-8")

            loader = ConfigLoader(config_path)
            with self.assertRaises(ConfigError):
                loader.load()


if __name__ == "__main__":
    unittest.main()

