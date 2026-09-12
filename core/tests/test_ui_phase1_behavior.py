# File: core/tests/test_ui_phase1_behavior.py

from __future__ import annotations

import importlib.util
import os
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest

    _PYSIDE_AVAILABLE = True
except ImportError:
    Qt = None  # type: ignore[assignment]
    QApplication = None  # type: ignore[assignment]
    QTest = None  # type: ignore[assignment]
    _PYSIDE_AVAILABLE = False

from core.application.contracts import IrisMessage, IrisResponse, IrisStatus, MessageRole
from core.application.contracts import ActionSuggestion, ConversationContent, DetailContent, TopicContext
from core.application.contracts import IrisEvent
from core.assistant.prompting import PROMPT_CANCEL_TOKEN, PromptRequest, PromptType


def _load_ui_module():
    module_name = "iris_ui_main_for_tests"
    module_path = Path(__file__).resolve().parents[2] / "ui" / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load UI module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wait_for(predicate, timeout_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication is not initialized")
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
        return IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, f"echo: {text}")],
            status=IrisStatus.COMPLETE,
        )

    def shutdown(self) -> None:
        return None


class _FailingIrisApplication(_FakeIrisApplication):
    def initialize(self, event_handler=None) -> None:
        raise RuntimeError("startup failed")


class _DummyWorker:
    def __init__(self, running: bool = False) -> None:
        self._running = running
        self.cancel_event = __import__("threading").Event()
        self.deleted = False
        self.prompt_cancelled = False
        self.last_prompt_response: str | None = None

    def isRunning(self) -> bool:
        return self._running

    def wait(self, timeout_ms: int) -> bool:
        _ = timeout_ms
        self._running = False
        return True

    def deleteLater(self) -> None:
        self.deleted = True

    def cancel_prompt(self) -> None:
        self.prompt_cancelled = True

    def provide_prompt_response(self, response_text: str) -> None:
        self.last_prompt_response = response_text


class _PromptBlockingIrisApplication(_FakeIrisApplication):
    def process_message(self, text: str, cancel_event=None, event_handler=None, prompt_provider=None):
        if prompt_provider is None:
            raise RuntimeError("prompt provider missing")
        prompt_provider("Root is not in config roots. Add and scan? [y/N]:")
        status = IrisStatus.CANCELLED if cancel_event is not None and cancel_event.is_set() else IrisStatus.COMPLETE
        return IrisResponse(messages=[], status=status)


class _EmptySettings:
    def value(self, _key: str):
        return None

    def setValue(self, _key: str, _value) -> None:
        return None


@unittest.skipUnless(_PYSIDE_AVAILABLE, "PySide6 is required for UI behavior tests")
class UiPhase1BehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _create_window(self, ui_module, app_cls) -> tuple[object, Path]:
        config_dir = Path(tempfile.mkdtemp(prefix="ui-test-"))
        config_path = config_dir / "config.json"
        config_path.write_text("{}\n", encoding="utf-8")
        with patch.object(ui_module, "IrisApplication", app_cls):
            with patch.object(ui_module.QMessageBox, "critical", return_value=None):
                window = ui_module.IrisWindow(config_path)
        return window, config_path

    def test_enter_submits(self) -> None:
        ui_module = _load_ui_module()
        widget = ui_module.ChatInput()
        hit = []
        widget.submitRequested.connect(lambda: hit.append(True))

        widget.show()
        widget.setFocus()
        QTest.keyClick(widget, Qt.Key.Key_Return)

        self.assertEqual(len(hit), 1)

    def test_shift_enter_inserts_newline(self) -> None:
        ui_module = _load_ui_module()
        widget = ui_module.ChatInput()
        hit = []
        widget.submitRequested.connect(lambda: hit.append(True))

        widget.show()
        widget.setPlainText("a")
        widget.setFocus()
        QTest.keyClick(widget, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)

        self.assertEqual(len(hit), 0)
        self.assertGreaterEqual(widget.document().blockCount(), 2)

    def test_send_ignored_while_worker_running(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        window.worker = _DummyWorker(running=True)
        window.input_box.setPlainText("hello")
        window.send_message()

        self.assertEqual(window.input_box.toPlainText(), "hello")
        window.close()

    def test_send_message_shows_thinking_line(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        class _SignalStub:
            def connect(self, _callback) -> None:
                return None

        class _DelayedWorker(_DummyWorker):
            def __init__(self) -> None:
                super().__init__(running=True)
                self.eventSignal = _SignalStub()
                self.finishedSignal = _SignalStub()
                self.failedSignal = _SignalStub()
                self.promptSignal = _SignalStub()
                self.finished = _SignalStub()

            def start(self) -> None:
                return None

        with patch.object(ui_module, "ProcessThread", side_effect=lambda *_args, **_kwargs: _DelayedWorker()):
            window.input_box.setPlainText("hello")
            window.send_message()

        history_text = window.history.toPlainText()
        self.assertIn("Thinking...", history_text)
        self.assertIn("hello", history_text)
        window.close()

    def test_stop_sets_cancellation_event(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        worker = _DummyWorker(running=True)
        window.worker = worker
        window.stop_request()

        self.assertTrue(worker.cancel_event.is_set())
        self.assertTrue(worker.prompt_cancelled)
        self.assertEqual(window.status_label.text(), "Stopping")
        window.close()

    def test_prompt_yes_no_cancel_records_history(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        worker = _DummyWorker(running=True)
        window.worker = worker

        prompt = PromptRequest(
            prompt_id="add-document-root",
            prompt_type=PromptType.YES_NO_CANCEL,
            text="Add this folder permanently and scan it?",
            choices=("yes", "no", "cancel"),
        )

        with patch.object(ui_module.QMessageBox, "question", return_value=ui_module.QMessageBox.StandardButton.No):
            window._on_worker_prompt(prompt)

        self.assertEqual(worker.last_prompt_response, "no")

        with patch.object(ui_module.QMessageBox, "question", return_value=ui_module.QMessageBox.StandardButton.Cancel):
            window._on_worker_prompt(prompt)

        self.assertEqual(worker.last_prompt_response, PROMPT_CANCEL_TOKEN)
        window.close()

    def test_prompt_cancel_releases_worker_wait(self) -> None:
        ui_module = _load_ui_module()
        app_service = _PromptBlockingIrisApplication(Path("."))
        worker = ui_module.ProcessThread(app_service, "/index scan D:/docs")

        prompt_seen = threading.Event()
        finished: list[IrisResponse] = []

        worker.promptSignal.connect(lambda _text: prompt_seen.set())
        worker.finishedSignal.connect(lambda response: finished.append(response))
        worker.start()

        self.assertTrue(_wait_for(lambda: prompt_seen.is_set(), timeout_seconds=2.0))
        worker.cancel_event.set()
        worker.cancel_prompt()
        self.assertTrue(worker.wait(1500))
        self.assertTrue(_wait_for(lambda: len(finished) == 1, timeout_seconds=2.0))
        self.assertEqual(finished[0].status, IrisStatus.CANCELLED)
        worker.deleteLater()

    def test_failed_request_restores_submitted_text(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        window.pending_text = "restore me"
        window.input_box.clear()
        window._on_worker_failed("boom")

        self.assertEqual(window.input_box.toPlainText(), "restore me")
        window.close()

    def test_render_response_prefers_conversation_summary(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        long_details = (
            "Raccoons (*Procyon lotor*) are fascinating mammals native to North America, known for their distinctive black mask, fluffy tails, and dexterous paws.\n\n"
            "### **Physical Traits**\n"
            "- **Appearance**: They have a sleek, stocky body, a black mask around their eyes, and a bushy tail.\n"
            "- **Size**: Adults weigh 10-25 pounds and measure 25-40 inches in length."
        )
        response = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, long_details)],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="Raccoons are adaptable North American mammals with black facial masks and strong problem-solving skills."),
            details=DetailContent(type="markdown", title="Supporting details", summary=long_details),
        )

        window._on_worker_finished(response)

        history_text = window.history.toPlainText()
        details_text = window.results_panel.current_text()

        self.assertIn("Raccoons are adaptable North American mammals", history_text)
        self.assertNotIn("### **Physical Traits**", history_text)
        self.assertIn("Physical Traits", details_text)
        self.assertNotIn("### **Physical Traits**", details_text)
        window.close()

    def test_activity_strip_shows_tools_and_model_after_a_turn(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))
        self.assertFalse(window.activity_label.isVisible())
        response = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, "done")],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="done"),
            metadata={"activity": {"actions": [{"name": "al_symbol", "status": "success", "target": "codeunit 80 Sales-Post"}], "model_calls": [{"model": "qwen2.5-coder", "prompt_tokens": 700, "completion_tokens": 50, "wall_ms": 1500}], "elapsed_ms": 2100}},
        )
        window._on_worker_finished(response)
        self.assertIn("Tools: al_symbol codeunit 80 Sales-Post (ok)", window.activity_label.text())
        self.assertIn("Model: qwen2.5-coder, 1 call, 750 tokens, 1.5 s", window.activity_label.text())
        self.assertIsNotNone(window.tray)
        self.assertEqual([action.text() for action in window.tray.icon.contextMenu().actions() if action.text()][0], "Show Iris")
        window._on_worker_finished(IrisResponse(messages=[], status=IrisStatus.COMPLETE))
        self.assertEqual(window.activity_label.text(), "")
        window.close()

    def test_first_run_starts_maximized_with_details_visible(self) -> None:
        ui_module = _load_ui_module()
        with patch.object(ui_module, "QSettings", side_effect=lambda *_args, **_kwargs: _EmptySettings()):
            window, _ = self._create_window(ui_module, _FakeIrisApplication)

        self.assertTrue(window._start_maximized)
        self.assertTrue(window.details_toggle.isChecked())
        window.show()
        self.assertTrue(_wait_for(lambda: window.details_panel.isVisible()))
        window.close()

    def test_details_view_is_text_selectable(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        flags = window.details_view.textInteractionFlags()
        self.assertTrue(bool(flags & Qt.TextInteractionFlag.TextSelectableByMouse))
        self.assertTrue(bool(flags & Qt.TextInteractionFlag.TextSelectableByKeyboard))
        window.close()

    def test_streamed_assistant_messages_do_not_update_chat_history(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        window._handle_event(IrisEvent(status=IrisStatus.THINKING, message=IrisMessage(MessageRole.ASSISTANT, "raw long assistant body")))

        self.assertNotIn("raw long assistant body", window.history.toPlainText())
        window.close()

    def test_details_structured_actions_render_in_explore_panel(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        details_text = "Platypus venom details"
        response = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, details_text)],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="Short answer."),
            topic=TopicContext(id="topic-1", title="Platypus", relationship="new_topic"),
            details=DetailContent(
                type="markdown",
                section_id="section-1",
                title="Platypus venom",
                content=details_text,
                summary=details_text,
                actions=[
                    ActionSuggestion(id="venom-humans", label="Effects on humans", payload={"command": "How does platypus venom affect humans?"}),
                    ActionSuggestion(id="venom-research", label="Research", payload={"command": "How do researchers study platypus venom?"}),
                ],
            ),
        )

        window._on_worker_finished(response)

        labels = [button.text() for button in window.details_action_buttons]
        self.assertEqual(labels, ["Effects on humans", "Research"])
        self.assertGreater(len(window.details_action_buttons), 0)
        window.close()

    def test_clicking_structured_details_action_button_submits_query(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        response = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, "Venom details")],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="Summary."),
            topic=TopicContext(id="topic-1", title="Platypus", relationship="new_topic"),
            details=DetailContent(
                type="markdown",
                section_id="section-1",
                title="Venom",
                content="Venom details",
                summary="Venom details",
                actions=[ActionSuggestion(id="venom-humans", label="Effects on humans", payload={"command": "How does platypus venom affect humans?"})],
            ),
        )

        window._on_worker_finished(response)

        target_button = next(button for button in window.details_action_buttons if button.text() == "Effects on humans")
        submitted: list[str] = []
        with patch.object(window, "_submit_command", side_effect=lambda command_text: submitted.append(command_text)):
            QTest.mouseClick(target_button, Qt.MouseButton.LeftButton)

        self.assertEqual(submitted, ["How does platypus venom affect humans?"])
        window.close()

    def test_explore_panel_accumulates_actions_without_duplicates(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        first = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, "Overview details")],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="Overview summary."),
            topic=TopicContext(id="topic-platypus", title="Platypus", relationship="new_topic"),
            details=DetailContent(
                type="markdown",
                section_id="section-1",
                title="Overview",
                content="Overview details",
                summary="Overview details",
                actions=[
                    ActionSuggestion(id="venom", label="Venom", payload={"command": "Tell me more about venom"}),
                    ActionSuggestion(id="ecosystem", label="Ecosystem", payload={"command": "Tell me more about ecosystem"}),
                ],
            ),
        )
        second = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, "Venom details")],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="Venom summary."),
            topic=TopicContext(id="topic-platypus", title="Platypus", relationship="continue"),
            details=DetailContent(
                type="markdown",
                section_id="section-2",
                title="Venom",
                content="Venom details",
                summary="Venom details",
                actions=[
                    ActionSuggestion(id="venom", label="Venom", payload={"command": "Tell me more about venom"}),
                    ActionSuggestion(id="effects", label="Effects on humans", payload={"command": "How does platypus venom affect humans?"}),
                ],
            ),
        )

        window._on_worker_finished(first)
        window._on_worker_finished(second)

        labels = [button.text() for button in window.details_action_buttons]
        self.assertEqual(labels, ["Venom", "Ecosystem", "Effects on humans"])
        window.close()

    def test_details_workspace_appends_on_continue_and_replaces_on_new_topic(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        first = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, "Platypus overview details")],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="Overview summary."),
            topic=TopicContext(id="topic-platypus", title="Platypus", relationship="new_topic"),
            details=DetailContent(type="markdown", section_id="section-1", title="Overview", content="Platypus overview details", summary="Platypus overview details"),
        )
        second = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, "Platypus venom details")],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="Venom summary."),
            topic=TopicContext(id="topic-platypus", title="Platypus", relationship="continue"),
            details=DetailContent(type="markdown", section_id="section-2", title="Venom", content="Platypus venom details", summary="Platypus venom details"),
        )
        third = IrisResponse(
            messages=[IrisMessage(MessageRole.ASSISTANT, "Llama overview details")],
            status=IrisStatus.COMPLETE,
            conversation=ConversationContent(message="Llama summary."),
            topic=TopicContext(id="topic-llama", title="Llama", relationship="new_topic"),
            details=DetailContent(type="markdown", section_id="section-3", title="Overview", content="Llama overview details", summary="Llama overview details"),
        )

        window._on_worker_finished(first)
        window._on_worker_finished(second)

        details_text = window.results_panel.current_text()
        self.assertIn("Platypus overview details", details_text)
        self.assertIn("Platypus venom details", details_text)

        window._on_worker_finished(third)
        llama_text = window.results_panel.current_text()
        self.assertIn("Llama overview details", llama_text)
        self.assertNotIn("Platypus venom details", llama_text)

        window.close()

    def test_startup_failure_disables_controls(self) -> None:
        ui_module = _load_ui_module()
        with patch.object(ui_module.QMessageBox, "critical", return_value=None):
            window, _ = self._create_window(ui_module, _FailingIrisApplication)

            self.assertTrue(_wait_for(lambda: window.init_worker is None))
            self.assertFalse(window.engine_available)
            self.assertFalse(window.input_box.isEnabled())
            self.assertFalse(window.send_button.isEnabled())
            window.close()

    def test_exit_response_closes_window_after_thread_stop(self) -> None:
        ui_module = _load_ui_module()
        window, _ = self._create_window(ui_module, _FakeIrisApplication)
        self.assertTrue(_wait_for(lambda: window.engine_available))

        closed = []
        worker = _DummyWorker(running=False)
        window.worker = worker
        with patch.object(window, "close", side_effect=lambda: closed.append(True)):
            response = IrisResponse(
                messages=[],
                status=IrisStatus.COMPLETE,
                response_type="session",
                details=DetailContent(metadata={"close_application": True}),
            )
            window._on_worker_finished(response)
            self.assertIs(window.worker, worker)
            window._on_worker_thread_stopped()
            self.assertTrue(_wait_for(lambda: len(closed) == 1))

        self.assertIsNone(window.worker)


if __name__ == "__main__":
    unittest.main()
