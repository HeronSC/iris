from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from core.assistant.action_commands import ActionCommandHandler
from core.assistant.index_commands import IndexCommandHandler
from core.assistant.prompting import PROMPT_CANCEL_TOKEN


@dataclass
class _ActionResult:
    status: str
    message: str
    error: str | None = None


class _FakeActionExecutor:
    def __init__(self) -> None:
        self.requests = []
        self.context = type("Ctx", (), {"web_shortcuts": {}})()

    def execute(self, request):
        self.requests.append(request)
        if request.action == "launch_application":
            return _ActionResult(status="failed", message="Unknown application: missing-app", error="Unknown application")
        if request.action == "open_url":
            return _ActionResult(status="failed", message="URL shortcut not found: missing-shortcut", error="URL shortcut not found")
        if request.action == "update_config":
            return _ActionResult(status="success", message="Config updated.")
        return _ActionResult(status="success", message="OK")

    def has_pending_confirmation(self) -> bool:
        return False

    def confirm_pending(self):
        return _ActionResult(status="success", message="Confirmed")

    def cancel_pending(self):
        return _ActionResult(status="success", message="Cancelled")

    def consume_expired_confirmation_notice(self) -> bool:
        return False


class _FakeAudit:
    def read_recent(self, limit: int = 10):
        _ = limit
        return []


class _FakeSearchContext:
    def get(self, number: int):
        _ = number
        return None


class _FakeCatalog:
    def count_documents(self) -> int:
        return 0

    def get_scan_state(self, key: str):
        _ = key
        return None

    def list_scan_errors(self, limit: int = 20):
        _ = limit
        return []


class _FakeScanner:
    def __init__(self, roots: list[Path]) -> None:
        self.config = type("Config", (), {"roots": roots})()
        self.scan_called = False

    def scan(self, root_filter=None, progress_callback=None):
        _ = root_filter
        _ = progress_callback
        self.scan_called = True
        return type(
            "ScanResult",
            (),
            {
                "scanned_files": 0,
                "indexed_files": 0,
                "updated_files": 0,
                "deleted_files": 0,
                "error_files": 0,
            },
        )()


class PromptCancelBehaviorTests(unittest.TestCase):
    def test_index_prompt_cancel_aborts_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scanner = _FakeScanner(roots=[])
            catalog = _FakeCatalog()
            outputs: list[str] = []

            handler = IndexCommandHandler(
                scanner,
                catalog,
                config_path=root / "config.json",
                output=lambda text, role: outputs.append(text),
                prompt_provider=lambda _prompt: PROMPT_CANCEL_TOKEN,
            )

            handled = handler.handle(f"/index scan {root}", {})

            self.assertTrue(handled)
            self.assertFalse(scanner.scan_called)
            self.assertTrue(any("scan cancelled" in text.lower() for text in outputs))

    def test_launch_prompt_cancel_stops_before_config_updates(self) -> None:
        outputs: list[str] = []
        executor = _FakeActionExecutor()
        handler = ActionCommandHandler(
            executor,
            _FakeAudit(),
            _FakeSearchContext(),
            output=lambda text, role: outputs.append(text),
            prompt_provider=lambda _prompt: PROMPT_CANCEL_TOKEN,
        )

        handled = handler.handle("/launch missing-app", {})

        self.assertTrue(handled)
        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(executor.requests[0].action, "launch_application")
        self.assertTrue(any("launch cancelled" in text.lower() for text in outputs))

    def test_open_url_prompt_cancel_stops_before_config_updates(self) -> None:
        outputs: list[str] = []
        executor = _FakeActionExecutor()
        handler = ActionCommandHandler(
            executor,
            _FakeAudit(),
            _FakeSearchContext(),
            output=lambda text, role: outputs.append(text),
            prompt_provider=lambda _prompt: PROMPT_CANCEL_TOKEN,
        )

        handled = handler.handle("/open-url missing-shortcut", {})

        self.assertTrue(handled)
        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(executor.requests[0].action, "open_url")
        self.assertTrue(any("open url cancelled" in text.lower() for text in outputs))


if __name__ == "__main__":
    unittest.main()
