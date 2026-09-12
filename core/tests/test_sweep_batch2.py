# File: core/tests/test_sweep_batch2.py

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.actions.implementations import system_info
from core.actions.implementations.memory_tools import MemoryNoteAction
from core.actions.implementations.screen_tools import ScreenLookAction
from core.actions.implementations.web_research import WebResearchAction
from core.actions.models import ActionRequest
from core.assistant.eval_command import EvalCommandHandler, classify, parse_expectation
from core.config.loader import ConfigError, ConfigLoader
from core.config.schema import check_config
from core.documents.extractors.eml_extractor import EmlExtractor
from core.knowledge.graph import KnowledgeGraph
from core.llm.models import LLMRequest, LLMResponse
from core.llm.ollama_client import OllamaClient
from core.storage.sqlite_database import SQLiteDatabase
from core.system import inventory
from core.system.limits import apply_process_priority, llm_options
from core.web.search import SearchHit, SearchResponse

HARDWARE_PAYLOAD = {
    "Manufacturer": "ASUS",
    "Model": "ROG",
    "TotalMemory": 68719476736,
    "OS": "Microsoft Windows 11 Pro",
    "OSVersion": "10.0.22631",
    "OSBuild": "22631",
    "InstallDate": "2024-01-15T10:00:00",
    "Cpu": "AMD Ryzen 9",
    "Cores": 16,
    "Threads": 32,
    "MaxClock": 4500,
    "Board": "ASUS X670",
    "Bios": "AMI 1.2",
    "Gpus": [{"Name": "NVIDIA RTX", "AdapterRAM": 25769803776, "DriverVersion": "32.0"}],
    "Memory": [{"Capacity": 34359738368, "Speed": 6000, "Manufacturer": "G.Skill"}],
}


class _Sink:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str, role: str | None = None) -> None:
        self.lines.append(text)

    def emit(self, text: str, role: str | None = None) -> None:
        self.lines.append(text)

    def __call__(self, text: str, role: str | None = None) -> None:
        self.lines.append(text)


class InventoryTests(unittest.TestCase):
    def test_hardware_shapes_the_probe(self) -> None:
        data = inventory.hardware(runner=lambda script: HARDWARE_PAYLOAD)
        self.assertEqual(data["cpu"], "AMD Ryzen 9")
        self.assertEqual(data["memory_total"], "64.0 GB")
        self.assertEqual(data["gpus"][0]["vram"], "24.0 GB")
        self.assertEqual(data["os_version"], "10.0.22631 build 22631")
        self.assertEqual(data["installed"], "2024-01-15")

    def test_installed_software_filters_by_name_or_publisher(self) -> None:
        rows = [
            {"DisplayName": "Visual Studio Code", "DisplayVersion": "1.93", "Publisher": "Microsoft", "InstallDate": "20240801"},
            {"DisplayName": "7-Zip", "DisplayVersion": "23.01", "Publisher": "Igor Pavlov", "InstallDate": ""},
        ]
        found = inventory.installed_software("microsoft", runner=lambda script: rows)
        self.assertEqual([row["name"] for row in found], ["Visual Studio Code"])
        self.assertEqual(len(inventory.installed_software(runner=lambda script: rows)), 2)

    def test_pending_updates_and_temperatures(self) -> None:
        updates = inventory.pending_updates(runner=lambda script: [{"Title": "Cumulative", "KB": "KB5001", "Severity": "Critical", "Size": 1048576, "Downloaded": True}])
        self.assertEqual(updates[0]["kb"], "KB5001")
        self.assertTrue(updates[0]["downloaded"])
        payload = {"Zones": [{"InstanceName": "ACPI\\ThermalZone\\TZ00_0", "CurrentTemperature": 3232}], "Fans": []}
        temps = inventory.temperatures(runner=lambda script: payload, gpu=lambda: [{"name": "RTX", "temperature_c": 51}])
        self.assertEqual(temps["zones"][0]["name"], "TZ00_0")
        self.assertEqual(temps["zones"][0]["celsius"], 50.1)
        self.assertEqual(temps["gpus"][0]["celsius"], 51)

    def test_largest_and_recent_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            big = Path(folder) / "big.bin"
            big.write_bytes(b"x" * 5000)
            small = Path(folder) / "sub" / "small.txt"
            small.parent.mkdir()
            small.write_text("hi", encoding="utf-8")
            old = time.time() - 3 * 86400
            os.utime(small, (old, old))
            largest, truncated = inventory.largest_files(folder, top=1)
            self.assertFalse(truncated)
            self.assertEqual(Path(largest[0]["path"]).name, "big.bin")
            recent, _ = inventory.recent_files(folder, hours=24)
            self.assertEqual([Path(row["path"]).name for row in recent], ["big.bin"])


class SystemToolTests(unittest.TestCase):
    def _run(self, action, arguments: dict) -> object:
        request = ActionRequest(action=action.name, arguments=arguments)
        validation = action.validate(request, SimpleNamespace())
        self.assertTrue(validation.ok, validation.error)
        return action.execute(ActionRequest(action=action.name, arguments=validation.resolved_arguments or arguments), SimpleNamespace())

    def test_hardware_info_tool_renders_table(self) -> None:
        with patch.object(inventory, "hardware", return_value=inventory.hardware(runner=lambda script: HARDWARE_PAYLOAD)):
            result = self._run(system_info.HardwareInfoAction(), {})
        self.assertEqual(result.status, "success")
        self.assertIn("AMD Ryzen 9", result.message)
        self.assertEqual(result.results[0].kind, "table")

    def test_installed_software_tool_reports_no_match(self) -> None:
        with patch.object(inventory, "installed_software", return_value=[]):
            result = self._run(system_info.InstalledSoftwareAction(), {"name": "nothing"})
        self.assertIn("No installed program matches", result.message)

    def test_temperatures_tool_explains_missing_sensors(self) -> None:
        with patch.object(inventory, "temperatures", return_value={"zones": [], "fans": [], "gpus": []}):
            result = self._run(system_info.TemperaturesAction(), {})
        self.assertIn("exposes no temperature", result.message)

    def test_new_tools_are_declared(self) -> None:
        names = {action.name for action in system_info.SYSTEM_ACTIONS}
        self.assertTrue({"hardware_info", "installed_software", "windows_updates", "temperatures", "largest_files", "recent_files"} <= names)


class WebResearchTests(unittest.TestCase):
    def test_reads_top_pages_and_numbers_sources(self) -> None:
        hits = (SearchHit(title="One", url="https://a.example/one", snippet="first"), SearchHit(title="Two", url="https://b.example/two"), SearchHit(title="Three", url="https://c.example/three"))
        client = SimpleNamespace(search=lambda query, **kwargs: SearchResponse(query=query, category="general", hits=hits))

        def fetch(url: str):
            if "b.example" in url:
                raise ValueError("blocked")
            return SimpleNamespace(title=f"Page {url[-3:]}", final_url=url, author=None, date="2026-09-01", text="Body text " * 20)

        action = WebResearchAction(client, SimpleNamespace(fetch=fetch))
        validation = action.validate(ActionRequest(action="web_research", arguments={"query": "  what is   iris ", "pages": 2}), SimpleNamespace())
        self.assertTrue(validation.ok)
        result = action.execute(ActionRequest(action="web_research", arguments=validation.resolved_arguments), SimpleNamespace())
        self.assertEqual(result.status, "success")
        self.assertIn("[1] Page one", result.message)
        self.assertIn("[2] Page ree", result.message)
        self.assertIn("Not read: https://b.example/two", result.message)
        self.assertIn("cite as [n]", result.message)
        kinds = [item.kind for item in result.results]
        self.assertEqual(kinds.count("link"), 2)
        self.assertIn("status", kinds)

    def test_rejects_bad_time_range(self) -> None:
        validation = WebResearchAction(SimpleNamespace(), SimpleNamespace()).validate(ActionRequest(action="web_research", arguments={"query": "x", "time_range": "decade"}), SimpleNamespace())
        self.assertFalse(validation.ok)


class MemoryNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.graph = KnowledgeGraph(SQLiteDatabase(Path(self._tmp.name) / "knowledge.db"))
        self.context = SimpleNamespace(knowledge=self.graph)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _note(self, arguments: dict):
        action = MemoryNoteAction()
        validation = action.validate(ActionRequest(action="memory_note", arguments=arguments), self.context)
        self.assertTrue(validation.ok, validation.error)
        return action.execute(ActionRequest(action="memory_note", arguments=validation.resolved_arguments), self.context)

    def test_keeps_fact_with_source_url(self) -> None:
        result = self._note({"topic": "Research/BC", "text": "BC 26 drops the classic client.", "url": "https://learn.microsoft.com/x"})
        self.assertEqual(result.status, "success")
        record = self.graph.records.get(result.resolved_target)
        self.assertEqual(record.topic, "research/bc")
        self.assertEqual(record.source_ref, "https://learn.microsoft.com/x")
        self.assertEqual(record.source, "web")
        again = self._note({"topic": "research/bc", "text": "BC 26 drops the classic client."})
        self.assertEqual(again.status, "failed")
        self.assertEqual(again.error, "duplicate")

    def test_requires_knowledge(self) -> None:
        validation = MemoryNoteAction().validate(ActionRequest(action="memory_note", arguments={"topic": "a", "text": "b"}), SimpleNamespace())
        self.assertFalse(validation.ok)


class EmlExtractorTests(unittest.TestCase):
    def test_extracts_headers_body_and_attachments(self) -> None:
        raw = (
            "From: Ann <ann@example.com>\r\nTo: Bob <bob@example.com>\r\nSubject: Quote\r\nDate: Mon, 01 Sep 2026 10:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"b1\"\r\n\r\n"
            "--b1\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<html><body><p>Hello <b>Bob</b>,</p><p>Price is &amp;pound;5.</p><style>p{}</style></body></html>\r\n"
            "--b1\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"quote.pdf\"\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--b1--\r\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mail.eml"
            path.write_bytes(raw.encode("utf-8"))
            extractor = EmlExtractor()
            self.assertTrue(extractor.supports(path))
            document = extractor.extract(path)
        self.assertEqual(document.content_status, "indexed")
        self.assertIn("Subject: Quote", document.text)
        self.assertIn("Hello Bob,", document.text)
        self.assertIn("Attachments: quote.pdf", document.text)
        self.assertNotIn("<b>", document.text)
        self.assertNotIn("p{}", document.text)


class ScreenLookTests(unittest.TestCase):
    def test_captures_and_asks_vision_model(self) -> None:
        seen: list[LLMRequest] = []

        def chat(request: LLMRequest) -> LLMResponse:
            seen.append(request)
            return LLMResponse(content="A code editor with a test file open.")

        def capture(destination: Path) -> Path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"\x89PNG fake")
            return destination

        with tempfile.TemporaryDirectory() as folder:
            context = SimpleNamespace(model_router=SimpleNamespace(chat=chat), captures_dir=Path(folder) / "Captures")
            action = ScreenLookAction(capture)
            validation = action.validate(ActionRequest(action="screen_look", arguments={"question": "what app is open?"}), context)
            self.assertTrue(validation.ok)
            result = action.execute(ActionRequest(action="screen_look", arguments=validation.resolved_arguments), context)
            self.assertEqual(result.status, "success")
            self.assertIn("code editor", result.message)
            self.assertEqual(seen[0].task, "vision")
            self.assertEqual(seen[0].messages[-1].images, (b"\x89PNG fake",))
            self.assertEqual(len(list((Path(folder) / "Captures").glob("screen-*.png"))), 1)
            gone = action.execute(ActionRequest(action="screen_look", arguments={"question": "x", "keep": False}), context)
            self.assertEqual(gone.status, "success")
            self.assertEqual(len(list((Path(folder) / "Captures").glob("screen-*.png"))), 1)

    def test_needs_router(self) -> None:
        validation = ScreenLookAction(lambda destination: destination).validate(ActionRequest(action="screen_look", arguments={}), SimpleNamespace())
        self.assertFalse(validation.ok)


class ConfigSchemaTests(unittest.TestCase):
    def test_unknown_keys_warn_and_wrong_types_error(self) -> None:
        errors, warnings = check_config({"assistant_name": "Iris", "model": 3, "mystery": True, "web": {"api_key": "abc"}})
        self.assertEqual(errors, ["'model' must be str, not int"])
        self.assertIn("Unknown setting 'mystery' is ignored", warnings)
        self.assertTrue(any("web.api_key" in item for item in warnings))

    def test_loader_rejects_wrong_type_and_keeps_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            base = {"assistant_name": "Iris", "memory_path": str(Path(folder) / "Data" / "Memory" / "memory.md"), "model": "qwen3:8b", "llm_server": "http://127.0.0.1:11434"}
            path.write_text(json.dumps({**base, "knowledge": "no"}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                ConfigLoader(path).load()
            path.write_text(json.dumps({**base, "made_up": 1, "llm": {"keep_alive": "30m"}, "resources": {"priority": "below_normal"}}), encoding="utf-8")
            loader = ConfigLoader(path)
            config = loader.load()
        self.assertEqual(config["llm"], {"keep_alive": "30m"})
        self.assertEqual(config["resources"], {"priority": "below_normal"})
        self.assertTrue(any("made_up" in item for item in config["config_warnings"]))


class ResourceLimitTests(unittest.TestCase):
    def test_llm_options_and_keep_alive_reach_ollama(self) -> None:
        options = llm_options({"llm": {"keep_alive": "30m", "num_thread": "8", "num_ctx": "abc"}})
        self.assertEqual(options, {"keep_alive": "30m", "num_thread": 8})
        client = OllamaClient("http://127.0.0.1:11434", "qwen3:8b", default_options=options)
        kwargs = client._chat_kwargs(LLMRequest.from_prompts("s", "u", options={"num_thread": 4}))
        self.assertEqual(kwargs["keep_alive"], "30m")
        self.assertEqual(kwargs["options"], {"num_thread": 4})
        self.assertNotIn("keep_alive", kwargs["options"])

    def test_priority_names(self) -> None:
        calls: list[int] = []
        applied = apply_process_priority("below normal", process=SimpleNamespace(nice=lambda level: calls.append(level)))
        self.assertEqual(applied, "below_normal")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(apply_process_priority("normal", process=SimpleNamespace(nice=lambda level: calls.append(level))))
        with self.assertRaises(ValueError):
            apply_process_priority("turbo")


class EvalCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "Evaluation"
        self.sink = _Sink()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _handler(self, classifier=None, retriever=None, knowledge=None) -> EvalCommandHandler:
        return EvalCommandHandler(self.folder, output=self.sink, classifier=classifier, retriever=retriever, knowledge=knowledge)

    def test_classify_without_pipeline(self) -> None:
        self.assertEqual(classify("hello there")["kind"], "small_talk")
        self.assertEqual(classify("write me a python function")["kind"], "code")
        self.assertEqual(parse_expectation("intent=find_files"), {"intent": "find_files"})
        with self.assertRaises(ValueError):
            parse_expectation("weird")

    def test_add_run_and_regressions(self) -> None:
        answers = {"hello": "small_talk", "find the budget": "find_files"}
        handler = self._handler(classifier=lambda text: {"kind": answers.get(text, "respond"), "intent": "respond"})
        self.assertTrue(handler.handle("/eval add small_talk :: hello", {}))
        self.assertTrue(handler.handle("/eval add find_files :: find the budget", {}))
        self.assertTrue(handler.handle("/eval add memory=printer :: which printer", {}))
        self.assertEqual(len(handler.cases()), 3)
        handler.handle("/eval", {})
        self.assertIn("2 of 3 passed.", self.sink.lines)
        answers["hello"] = "respond"
        self.sink.lines.clear()
        handler.handle("/eval", {})
        self.assertIn("1 of 3 passed.", self.sink.lines)
        self.assertTrue(any("1 regression(s)" in line and "hello" in line for line in self.sink.lines))

    def test_memory_expectation_uses_retriever(self) -> None:
        retriever = SimpleNamespace(retrieve=lambda query: SimpleNamespace(as_records=lambda: [SimpleNamespace(content="The office printer is the HP on the second floor.")]))
        handler = self._handler(classifier=lambda text: {"kind": "respond", "intent": "respond"}, retriever=retriever)
        handler.add_case("which printer do we use", {"memory": "HP on the second floor"})
        report = handler.run()
        self.assertTrue(report["results"][0]["ok"])

    def test_failures_lists_corrections_and_gaps(self) -> None:
        record = SimpleNamespace(created_at="2026-09-10T10:00:00", content="Use 127.0.0.1 not localhost")
        knowledge = SimpleNamespace(open_observations=lambda topic: [record] if "corrections" in topic else [])
        handler = self._handler(knowledge=knowledge)
        handler.handle("/eval failures", {})
        self.assertTrue(any("1 correction(s) and 0 open question(s)" in line for line in self.sink.lines))


if __name__ == "__main__":
    unittest.main()
