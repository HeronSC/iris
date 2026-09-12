# File: core/tests/test_ui_streaming.py

from __future__ import annotations

import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    _PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover - environment without Qt
    QApplication = None  # type: ignore[assignment]
    _PYSIDE_AVAILABLE = False

from core.application.contracts import ConversationContent, IrisEvent, IrisMessage, IrisResponse, IrisStatus, MessageRole


def _load_ui_module():
    module_path = Path(__file__).resolve().parents[2] / "ui" / "main.py"
    spec = importlib.util.spec_from_file_location("iris_ui_main_for_stream_tests", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wait_for(predicate, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    app = QApplication.instance()
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return False


class _FakeIrisApplication:
    def __init__(self, config_path: Path, prompt_provider=None) -> None:
        self.config_path = config_path
        self.prompt_provider = prompt_provider
        self.config = {"assistant_name": "Iris"}
        self.startup_messages: list[IrisMessage] = []

    def initialize(self, event_handler=None) -> None:
        self.startup_messages = [IrisMessage(MessageRole.SYSTEM, "Iris is ready.")]

    def process_message(self, text: str, cancel_event=None, event_handler=None, prompt_provider=None):
        return IrisResponse(messages=[IrisMessage(MessageRole.ASSISTANT, f"echo: {text}")], status=IrisStatus.COMPLETE)

    def shutdown(self) -> None:
        return None


@unittest.skipUnless(_PYSIDE_AVAILABLE, "PySide6 is required for UI behavior tests")
class UiStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        ui_module = _load_ui_module()
        config_path = Path(tempfile.mkdtemp(prefix="ui-stream-")) / "config.json"
        config_path.write_text("{}\n", encoding="utf-8")
        with patch.object(ui_module, "IrisApplication", _FakeIrisApplication):
            with patch.object(ui_module.QMessageBox, "critical", return_value=None):
                window = ui_module.IrisWindow(config_path)
        # Let the initialisation thread finish before the test drives or closes the window;
        # Qt aborts the process if a QThread is destroyed while still running.
        self.assertTrue(_wait_for(lambda: window.engine_available))
        return window

    def test_deltas_replace_the_placeholder_and_the_final_answer_replaces_the_stream(self) -> None:
        window = self._window()
        window._append_message(MessageRole.USER, "what about the pump?")
        window._placeholder_block = window.history.document().blockCount()
        window._stream_text = ""
        window._append_message(MessageRole.ASSISTANT, "Thinking...", label="Iris")
        before = window.history.toPlainText()
        self.assertIn("Thinking...", before)

        window._handle_event(IrisEvent(status=IrisStatus.THINKING, delta="The pump "))
        window._handle_event(IrisEvent(status=IrisStatus.THINKING, delta="filter."))
        streamed = window.history.toPlainText()
        self.assertNotIn("Thinking...", streamed)
        self.assertEqual(streamed.count("The pump filter."), 1)
        self.assertIn("what about the pump?", streamed)

        response = IrisResponse(messages=[], status=IrisStatus.COMPLETE, conversation=ConversationContent(message="Replace it twice a year."))
        window._render_response(response)
        final = window.history.toPlainText()
        self.assertNotIn("The pump filter.", final)
        self.assertEqual(final.count("Replace it twice a year."), 1)
        self.assertEqual(final.count("what about the pump?"), 1)
        window.close()

    def test_without_streaming_the_answer_is_appended_as_before(self) -> None:
        window = self._window()
        window._placeholder_block = window.history.document().blockCount()
        window._stream_text = ""
        window._append_message(MessageRole.ASSISTANT, "Thinking...", label="Iris")
        window._render_response(IrisResponse(messages=[], status=IrisStatus.COMPLETE, conversation=ConversationContent(message="Done.")))
        text = window.history.toPlainText()
        self.assertIn("Thinking...", text)
        self.assertIn("Done.", text)
        window.close()


if __name__ == "__main__":
    unittest.main()
