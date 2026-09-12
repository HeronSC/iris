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
except Exception:
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
        self.assertEqual(labels, ["Show Iris", "Hide to tray", "Close to tray instead of quitting", "Quit Iris"])
        shown: list[int] = []
        tray.showRequested.connect(lambda: shown.append(1))
        tray.close_action.setChecked(True)
        self.assertTrue(tray.close_to_tray)
        tray.show_action.trigger()
        self.assertEqual(shown, [1])
        self.assertFalse(tray.icon.icon().isNull())


if __name__ == "__main__":
    unittest.main()
