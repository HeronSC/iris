# File: core/tests/test_watchers.py

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core.assistant.watch_command import WatchCommandHandler
from core.watchers import InboxNotifier, Notification, QuietHours, WatcherDefinition, WatcherService, parse_duration
from core.watchers import checks
from core.watchers.checks import CheckResult, run_check


class FakeNotifier:
    def __init__(self, name: str = "toast", fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        if self.fail:
            raise RuntimeError("toast broke")
        self.sent.append(notification)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class ModelTests(unittest.TestCase):
    def test_durations(self) -> None:
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("5m"), 300)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration(90), 90)
        with self.assertRaises(ValueError):
            parse_duration("soon")

    def test_definition_round_trip(self) -> None:
        definition = WatcherDefinition(kind="disk_free_below", params={"mount": "E:", "gb": 50}, name="E drive", interval_seconds=120, channels=("inbox",))
        restored = WatcherDefinition.from_json(definition.to_json())
        self.assertEqual(restored, definition)
        self.assertEqual(WatcherDefinition(kind="path_missing", params={"path": "x"}).label, "path_missing path=x")

    def test_quiet_hours(self) -> None:
        quiet = QuietHours.parse("22:00-07:00")
        self.assertTrue(quiet.active(datetime(2026, 9, 10, 23, 30)))
        self.assertTrue(quiet.active(datetime(2026, 9, 11, 6, 59)))
        self.assertFalse(quiet.active(datetime(2026, 9, 11, 7, 0)))
        self.assertFalse(quiet.active(datetime(2026, 9, 11, 12, 0)))
        self.assertTrue(QuietHours.parse("12:00-13:00").active(datetime(2026, 9, 11, 12, 30)))
        self.assertFalse(QuietHours().active())
        self.assertEqual(str(quiet), "22:00-07:00")
        with self.assertRaises(ValueError):
            QuietHours.parse("late")


class CheckTests(unittest.TestCase):
    def test_disk_free_below_has_hysteresis(self) -> None:
        class Usage:
            total = 1000 * 1024**3
            free = 15 * 1024**3
            percent = 98.5

        with patch("psutil.disk_usage", return_value=Usage()):
            result = run_check("disk_free_below", {"mount": "E", "gb": 20}, None)
        self.assertTrue(result.triggered)
        self.assertIn("E:\\ has 15.0 GB free", result.summary)
        self.assertFalse(result.clear_when(21.0), "just above the limit is not clear yet")
        self.assertTrue(result.clear_when(23.0))

    def test_service_not_running_and_path_checks(self) -> None:
        with patch.object(checks.probes, "services", return_value=[{"name": "Spooler", "status": "Stopped"}]):
            self.assertTrue(run_check("service_not_running", {"name": "spooler"}, None).triggered)
        with patch.object(checks.probes, "services", return_value=[]):
            result = run_check("service_not_running", {"name": "Ghost"}, None)
            self.assertTrue(result.triggered)
            self.assertIn("not installed", result.summary)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "watched.txt"
            target.write_text("a", encoding="utf-8")
            first = run_check("path_changed", {"path": str(target)}, None)
            self.assertFalse(first.triggered)
            self.assertIsNotNone(first.value)
            same = run_check("path_changed", {"path": str(target)}, first.value)
            self.assertFalse(same.triggered)
            target.write_text("a longer text", encoding="utf-8")
            changed = run_check("path_changed", {"path": str(target)}, first.value)
            self.assertTrue(changed.triggered)
            self.assertTrue(run_check("path_missing", {"path": str(Path(tmp) / "nope")}, None).triggered)
        with self.assertRaises(ValueError):
            run_check("no_such_kind", {}, None)

    def test_event_log_errors_only_report_new_events(self) -> None:
        rows = [{"time": "2026-09-10T15:54", "source": "Application Error", "id": 1000, "message": "python.exe faulted"}]
        with patch.object(checks.probes, "event_log_errors", return_value=rows):
            fresh = run_check("event_log_errors", {"hours": 1}, None)
            self.assertTrue(fresh.triggered)
            self.assertEqual(fresh.value, "2026-09-10T15:54")
            seen = run_check("event_log_errors", {"hours": 1}, "2026-09-10T15:54")
            self.assertFalse(seen.triggered)
        with patch.object(checks.probes, "event_log_errors", return_value=[]):
            self.assertFalse(run_check("event_log_errors", {}, None).triggered)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.toast = FakeNotifier("toast")
        self.inbox = InboxNotifier(root / "inbox.jsonl")
        self.clock = Clock(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))
        self.service = WatcherService(root / "watchers.json", root / "state.json", {"toast": self.toast}, inbox=self.inbox, clock=self.clock)
        self.results: dict[str, CheckResult] = {}
        self._patch = patch(
            "core.watchers.service.run_check",
            side_effect=lambda kind, params, baseline, context=None: self.results[kind],
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.service.stop()
        self._tmp.cleanup()

    def _add(self, kind: str = "memory_percent_above", **overrides) -> WatcherDefinition:
        return self.service.add(WatcherDefinition(kind=kind, params={"percent": 90}, name="RAM", **overrides))

    def test_transition_dedupe_renotify_and_clear(self) -> None:
        definition = self._add(renotify_minutes=30)
        self.results["memory_percent_above"] = CheckResult(False, "RAM at 40%", 40.0)
        self.assertEqual(self.service.evaluate(definition.id), [])
        self.results["memory_percent_above"] = CheckResult(True, "RAM at 95%", 95.0, clear_when=lambda v: v <= 85)
        sent = self.service.evaluate(definition.id)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].title, "RAM")
        self.assertEqual(sent[0].delivered_to, ("toast", "inbox"))
        self.assertEqual(len(self.toast.sent), 1)

        self.clock.advance(minutes=10)
        self.assertEqual(self.service.evaluate(definition.id), [], "still true, not yet due again")
        self.clock.advance(minutes=25)
        self.assertEqual(len(self.service.evaluate(definition.id)), 1, "re-notified after the interval")

        self.results["memory_percent_above"] = CheckResult(False, "RAM at 88%", 88.0, clear_when=lambda v: v <= 85)
        self.assertEqual(self.service.evaluate(definition.id), [], "inside the hysteresis band stays active")
        self.assertTrue(self.service.state(definition.id).active)
        self.results["memory_percent_above"] = CheckResult(False, "RAM at 60%", 60.0, clear_when=lambda v: v <= 85)
        cleared = self.service.evaluate(definition.id)
        self.assertEqual(len(cleared), 1)
        self.assertTrue(cleared[0].cleared)
        self.assertFalse(self.service.state(definition.id).active)
        self.assertEqual(self.inbox.recent(10)[0].title, "RAM — cleared")

    def test_first_run_of_a_baseline_kind_never_notifies(self) -> None:
        definition = self._add(kind="path_changed")
        self.results["path_changed"] = CheckResult(False, "Watching", {"mtime": 1})
        self.assertEqual(self.service.evaluate(definition.id), [])
        self.assertEqual(self.service.state(definition.id).baseline, {"mtime": 1})
        self.results["path_changed"] = CheckResult(True, "changed", {"mtime": 2})
        self.assertEqual(len(self.service.evaluate(definition.id)), 1)
        self.assertEqual(self.service.state(definition.id).baseline, {"mtime": 2}, "the new signature becomes the baseline")

    def test_quiet_hours_hold_toasts_and_digest_them(self) -> None:
        self.service.quiet_hours = QuietHours.parse("00:00-23:59")
        definition = self._add()
        self.results["memory_percent_above"] = CheckResult(True, "RAM at 95%", 95.0)
        sent = self.service.evaluate(definition.id)
        self.assertEqual(sent[0].delivered_to, ("inbox",))
        self.assertTrue(sent[0].deferred)
        self.assertEqual(self.toast.sent, [])
        self.assertEqual(len(self.service.missed()), 1)
        digest = self.service.flush_digest()
        self.assertIn("While you were away: 1 alert", digest.title)
        self.assertEqual(len(self.toast.sent), 1)
        self.assertEqual(self.service.missed(), [])

    def test_failed_checks_and_notifiers_are_recorded_not_raised(self) -> None:
        definition = self._add()
        self._patch.stop()
        with patch("core.watchers.service.run_check", side_effect=RuntimeError("probe exploded")):
            self.assertEqual(self.service.evaluate(definition.id), [])
        self._patch.start()
        state = self.service.state(definition.id)
        self.assertEqual(state.failures, 1)
        self.assertIn("probe exploded", state.last_error)
        self.toast.fail = True
        self.results["memory_percent_above"] = CheckResult(True, "RAM at 95%", 95.0)
        sent = self.service.evaluate(definition.id)
        self.assertEqual(sent[0].delivered_to, ("inbox",))

    def test_definitions_and_state_survive_a_restart(self) -> None:
        definition = self._add(interval_seconds=120)
        self.results["memory_percent_above"] = CheckResult(True, "RAM at 95%", 95.0)
        self.service.evaluate(definition.id)
        reloaded = WatcherService(self.service.definitions_path, self.service.state_path, {"toast": FakeNotifier()}, inbox=self.inbox, clock=self.clock)
        self.assertEqual([item.id for item in reloaded.definitions()], [definition.id])
        self.assertTrue(reloaded.state(definition.id).active)
        self.assertEqual(reloaded.state(definition.id).notifications, 1)

    def test_unknown_kind_or_channel_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.service.add(WatcherDefinition(kind="nope"))
        with self.assertRaises(ValueError):
            self.service.add(WatcherDefinition(kind="path_missing", channels=("pager",)))

    def test_scheduler_runs_and_stops(self) -> None:
        definition = self._add(interval_seconds=5)
        self.service.start()
        self.assertTrue(self.service.running)
        self.assertIsNotNone(self.service._scheduler.get_job(f"watcher-{definition.id}"))
        self.service.set_enabled(definition.id, False)
        self.assertIsNone(self.service._scheduler.get_job(f"watcher-{definition.id}"))
        self.service.set_enabled(definition.id, True)
        self.assertIsNotNone(self.service._scheduler.get_job(f"watcher-{definition.id}"))
        self.service.stop()
        self.assertFalse(self.service.running)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.toast = FakeNotifier("toast")
        self.service = WatcherService(root / "watchers.json", root / "state.json", {"toast": self.toast}, inbox=InboxNotifier(root / "inbox.jsonl"))
        self.output: list[str] = []
        self.handler = WatchCommandHandler(self.service, output=lambda text, role=None: self.output.append(text))

    def tearDown(self) -> None:
        self.service.stop()
        self._tmp.cleanup()

    def test_add_list_run_pause_remove(self) -> None:
        self.assertFalse(self.handler.handle("/watches", {}))
        self.assertTrue(self.handler.handle('/watch add path_missing path="C:\\nope\\gone.txt" every=2m name="Gone file" channels=inbox', {}))
        self.assertIn("Watching: Gone file every 2m", self.output[-1])
        definition = self.service.definitions()[0]
        self.assertEqual(definition.params, {"path": "C:\\nope\\gone.txt"})
        self.assertEqual(definition.interval_seconds, 120)
        self.assertEqual(definition.channels, ("inbox",))

        self.handler.handle("/watch", {})
        self.assertIn("Gone file every 2m -> inbox", self.output[-1])
        self.handler.handle(f"/watch run {definition.id}", {})
        self.assertIn("ACTIVE", self.output[-1])
        self.assertIn("is missing", self.output[-1])
        self.handler.handle("/watch inbox", {})
        self.assertIn("Gone file", self.output[-1])
        self.handler.handle(f"/watch pause {definition.id[:4]}", {})
        self.assertIn("Paused", self.output[-1])
        self.handler.handle(f"/watch remove {definition.id}", {})
        self.assertIn("Removed", self.output[-1])
        self.assertEqual(self.service.definitions(), [])

    def test_kinds_quiet_test_and_errors(self) -> None:
        self.handler.handle("/watch kinds", {})
        self.assertIn("disk_free_below", self.output[-1])
        self.handler.handle("/watch quiet 22:00-07:00", {})
        self.assertIn("22:00-07:00", self.output[-1])
        self.handler.handle("/watch quiet off", {})
        self.assertIn("off", self.output[-1])
        self.handler.handle("/watch test", {})
        self.assertEqual(len(self.toast.sent), 1)
        self.handler.handle("/watch add bogus", {})
        self.assertIn("Unknown watcher kind", self.output[-1])
        self.handler.handle("/watch add disk_free_below mount", {})
        self.assertIn("key=value", self.output[-1])
        self.handler.handle("/watch bogus", {})
        self.assertIn("Usage", self.output[-1])


if __name__ == "__main__":
    unittest.main()
