import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().with_name("loader.py")
spec = importlib.util.spec_from_file_location("memory_loader", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
AssistantMemoryError = module.AssistantMemoryError
MemoryLoader = module.MemoryLoader


class AssistantMemoryLoaderTests(unittest.TestCase):
    def test_bootstraps_when_required_files_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = MemoryLoader(tmpdir)
            memory = loader.load()

            self.assertIn("profile", memory)
            self.assertIn("preferences", memory)
            self.assertIn("projects", memory)
            self.assertIn("knowledge", memory)
            self.assertEqual(memory["profile"].get("profile"), {})
            self.assertEqual(memory["preferences"].get("preferences"), [])
            self.assertEqual(memory["projects"].get("projects"), [])
            self.assertEqual(memory["knowledge"].get("knowledge_areas"), [])

    def test_loads_existing_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            for name in ["profile.json", "preferences.json", "projects.json", "knowledge.json"]:
                (folder / name).write_text(json.dumps({}), encoding="utf-8")

            loader = MemoryLoader(folder)
            memory = loader.load()

            self.assertEqual(memory["profile"], {})
            self.assertEqual(memory["preferences"], {})
            self.assertEqual(memory["projects"], {})
            self.assertEqual(memory["knowledge"], {})

    def test_raises_when_memory_path_is_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_path = Path(tmpdir) / "memory.json"
            memory_path.write_text("{}", encoding="utf-8")
            loader = MemoryLoader(memory_path)

            with self.assertRaises(AssistantMemoryError):
                loader.load()


if __name__ == "__main__":
    unittest.main()
