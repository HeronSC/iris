# File: core/tests/test_activity_and_desktop.py

"""Tool and action status in the window (6), the tray icon and the global hotkey."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.assistant.why_command import WhyCommandHandler, describe_activity

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    _PYSIDE_AVAILABLE = True
except ImportError:
    QApplication = None
    _PYSIDE_AVAILABLE = False


class _Stream:
    def __init__(self, events):
        self.events = events

    def read(self, *, category=None, request_id=None, limit=None):
        return [event for event in self.events if event.category == category and event.request_id == request_id]


class _Metrics:
    def __init__(self, rows):
        self.rows = rows

    def recent(self, limit=20):
        return list(self.rows)


class _ActionAudit:
    def __init__(self, rows):
        self.rows = rows

    def read_recent(self, limit=200):
        return list(self.rows)


class SummaryTests(unittest.TestCase):
    def test_summary_gathers_model_calls_actions_tools_and_permissions_for_one_request(self) -> None:
        from core.audit.stream import AuditCategory

        tool_event = SimpleNamespace(category=AuditCategory.TOOL, request_id="r1", event="weather", status="success", subject="capability", error=None, target=None, message=None)
        other_event = SimpleNamespace(category=AuditCategory.TOOL, request_id="r2", event="news", status="success", subject="capability", error=None, target=None, message=None)
        denied = SimpleNamespace(category=AuditCategory.PERMISSION, request_id="r1", event="clipboard", status="denied", subject=None, error=None, target="clipboard", message="outbound cap")
        handler = WhyCommandHandler(
            log_file=None,
            trace_file=None,
            metrics=_Metrics([{"request_id": "r1", "task": "intent", "model": "qwen3:8b", "prompt_tokens": 500, "completion_tokens": 40, "wall_ms": 900.0, "outcome": "ok", "fallback": False}, {"request_id": "r2", "model": "x"}]),
            action_audit=_ActionAudit([{"request_id": "r1", "action": "al_symbol", "tool": "al_symbol", "status": "success", "resolved_target": "codeunit Sales-Post"}]),
            audit_stream=_Stream([tool_event, other_event, denied]),
            last_request_id=lambda: "r1",
            output=None,
        )
        summary = handler.summary("r1")
        self.assertEqual([item["name"] for item in summary["tools"]], ["weather"])
        self.assertEqual(summary["actions"][0]["target"], "codeunit Sales-Post")
        self.assertEqual(summary["model_calls"][0]["model"], "qwen3:8b")
        self.assertEqual(summary["permissions"][0]["status"], "denied")
        self.assertEqual(handler.summary(None), {})
        line = describe_activity(summary)
        self.assertIn("Tools: al_symbol codeunit Sales-Post (ok), weather (ok)", line)
        self.assertIn("Model: qwen3:8b, 1 call, 540 tokens, 0.9 s", line)
        self.assertIn("Blocked: clipboard", line)
        self.assertEqual(describe_activity(None), "")
        self.assertEqual(describe_activity({}), "")

    def test_a_pending_action_reads_as_waiting(self) -> None:
        line = describe_activity({"actions": [{"name": "update_config", "status": "pending_confirmation"}], "elapsed_ms": 2200})
        self.assertEqual(line, "Tools: update_config (waiting) · Total 2.2 s")


@unittest.skipUnless(_PYSIDE_AVAILABLE, "PySide6 is required")
class DesktopExtrasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        module_path = Path(__file__).resolve().parents[2] / "ui" / "desktop_extras.py"
        spec = importlib.util.spec_from_file_location("iris_desktop_extras_for_tests", module_path)
        cls.extras = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.extras)

    def test_hotkeys_parse_and_reject_nonsense(self) -> None:
        extras = self.extras
        self.assertEqual(extras.parse_hotkey("ctrl+alt+i"), (0x0003, ord("I")))
        self.assertEqual(extras.parse_hotkey("Win+Shift+F9"), (0x000C, 0x78))
        for bad in ("", "i", "ctrl+", "ctrl+bogus"):
            with self.assertRaises(ValueError):
                extras.parse_hotkey(bad)

    def test_registration_uses_the_given_registrar_and_reports_failure(self) -> None:
        extras = self.extras
        calls: list[tuple[int, int, int]] = []
        pressed: list[int] = []
        hotkey = extras.GlobalHotkey(lambda: pressed.append(1), register=lambda hotkey_id, modifiers, key: calls.append((hotkey_id, modifiers, key)) or True, unregister=lambda hotkey_id: calls.append((hotkey_id, -1, -1)))
        self.assertTrue(hotkey.register("ctrl+alt+i"))
        self.assertEqual(calls[-1], (extras.HOTKEY_ID, 0x0003, ord("I")))
        hotkey.unregister()
        self.assertEqual(calls[-1], (extras.HOTKEY_ID, -1, -1))
        self.assertFalse(hotkey.registered)
        taken = extras.GlobalHotkey(lambda: None, register=lambda *_: False, unregister=lambda _: None)
        self.assertFalse(taken.register("ctrl+alt+i"))
        self.assertFalse(extras.GlobalHotkey(lambda: None, register=lambda *_: True, unregister=lambda _: None).register("nonsense"))

    def test_the_tray_menu_offers_show_hide_close_to_tray_and_quit(self) -> None:
        extras = self.extras
        from PySide6.QtWidgets import QWidget

        parent = QWidget()
        tray = extras.TrayController(parent, close_to_tray=False)
        labels = [action.text() for action in tray.icon.contextMenu().actions() if action.text()]
        self.assertEqual(labels, ["Show Iris", "Hide to tray", "Close to tray instead of quitting", "Mute voice", "Quit Iris"])
        shown: list[int] = []
        tray.showRequested.connect(lambda: shown.append(1))
        tray.close_action.setChecked(True)
        self.assertTrue(tray.close_to_tray)
        tray.show_action.trigger()
        self.assertEqual(shown, [1])
        self.assertFalse(tray.icon.icon().isNull())
        muted: list[bool] = []
        tray.muteToggled.connect(lambda checked: muted.append(bool(checked)))
        tray.mute_action.setChecked(True)
        self.assertEqual(muted, [True])
        self.assertTrue(extras.TrayController(parent, voice_muted=True).mute_action.isChecked())

    def test_the_talk_hotkey_registers_under_its_own_id(self) -> None:
        extras = self.extras
        calls: list[tuple[int, int, int]] = []
        talk = extras.GlobalHotkey(lambda: None, hotkey_id=extras.TALK_HOTKEY_ID, register=lambda hotkey_id, modifiers, key: calls.append((hotkey_id, modifiers, key)) or True, unregister=lambda hotkey_id: calls.append((hotkey_id, -1, -1)))
        self.assertTrue(talk.register("ctrl+alt+t"))
        self.assertEqual(calls[-1], (extras.TALK_HOTKEY_ID, 0x0003, ord("T")))
        self.assertEqual(talk.key, ord("T"))
        self.assertNotEqual(extras.TALK_HOTKEY_ID, extras.HOTKEY_ID)
        talk.unregister()
        self.assertEqual(calls[-1], (extras.TALK_HOTKEY_ID, -1, -1))


class _FakeVoiceService:
    def __init__(self, text: str = "what time is it", *, available: bool = True) -> None:
        self.text = text
        self.available = available
        self.problem = None if available else "Voice needs sounddevice installed."
        self.recorder = SimpleNamespace(full=False, active=False)
        self.speaking = False
        self.stopped = 0
        self.cancelled = 0

    def start_listening(self) -> None:
        self.recorder.active = True

    def stop_listening(self) -> str:
        self.recorder.active = False
        return self.text

    def cancel_listening(self) -> None:
        self.cancelled += 1
        self.recorder.active = False

    def stop_speaking(self) -> None:
        self.stopped += 1
        self.speaking = False


@unittest.skipUnless(_PYSIDE_AVAILABLE, "PySide6 is not installed")
class VoiceControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        module_path = Path(__file__).resolve().parents[2] / "ui" / "voice_controls.py"
        spec = importlib.util.spec_from_file_location("iris_voice_controls_for_tests", module_path)
        cls.controls = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.controls)

    def _controller(self, service, *, key_down, clock):
        controller = self.controls.VoiceController(service, key_is_down=key_down, clock=clock)
        self.heard: list[str] = []
        self.states: list[str] = []
        self.failures: list[str] = []
        controller.transcribed.connect(self.heard.append)
        controller.stateChanged.connect(self.states.append)
        controller.failed.connect(self.failures.append)
        return controller

    def _wait(self, controller) -> None:
        worker = controller._worker
        if worker is not None:
            worker.wait(5000)
        for _ in range(20):
            self.app.processEvents()

    def test_holding_the_key_records_until_release(self) -> None:
        service = _FakeVoiceService()
        now = [0.0]
        down = [True]
        controller = self._controller(service, key_down=lambda: down[0], clock=lambda: now[0])
        controller.on_hotkey()
        self.assertTrue(controller.listening)
        self.assertTrue(service.recorder.active)
        controller.poll()
        self.assertTrue(controller.listening)
        now[0] = 1.5
        down[0] = False
        controller.poll()
        self.assertTrue(controller.hold_mode)
        self.assertEqual(controller.state, "transcribing")
        self._wait(controller)
        self.assertEqual(self.heard, ["what time is it"])
        self.assertEqual(self.states, ["listening", "transcribing", "idle"])

    def test_a_quick_tap_toggles_and_the_next_press_sends(self) -> None:
        service = _FakeVoiceService()
        now = [0.0]
        controller = self._controller(service, key_down=lambda: False, clock=lambda: now[0])
        controller.on_hotkey()
        now[0] = 0.1
        controller.poll()
        self.assertTrue(controller.listening)
        self.assertFalse(controller.hold_mode)
        self.assertFalse(controller.timer.isActive())
        controller.on_hotkey()
        self.assertEqual(controller.state, "transcribing")
        self._wait(controller)
        self.assertEqual(self.heard, ["what time is it"])

    def test_silence_and_unavailable_voice_are_reported(self) -> None:
        silent = _FakeVoiceService("")
        controller = self._controller(silent, key_down=lambda: False, clock=lambda: 0.0)
        controller.on_hotkey()
        controller.on_hotkey()
        self._wait(controller)
        self.assertEqual(self.heard, [])
        self.assertEqual(self.failures, ["Iris did not hear anything."])
        missing = self._controller(_FakeVoiceService(available=False), key_down=lambda: False, clock=lambda: 0.0)
        missing.on_hotkey()
        self.assertEqual(self.failures, ["Voice needs sounddevice installed."])
        self.assertEqual(missing.state, "idle")

    def test_the_hotkey_interrupts_speech_and_cancel_drops_the_recording(self) -> None:
        service = _FakeVoiceService()
        service.speaking = True
        controller = self._controller(service, key_down=lambda: False, clock=lambda: 0.0)
        self.assertTrue(controller.stop_speaking())
        self.assertEqual(service.stopped, 1)
        controller.on_hotkey()
        controller.cancel()
        self.assertEqual(service.cancelled, 1)
        self.assertEqual(controller.state, "idle")


if __name__ == "__main__":
    unittest.main()
