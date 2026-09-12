# File: core/watchers/service.py

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.watchers.checks import KINDS, CheckResult, run_check
from core.watchers.models import Notification, WatcherContext, WatcherDefinition, WatcherState, format_duration, utc_now_iso
from core.watchers.notify import InboxNotifier, Notifier, QuietHours

logger = logging.getLogger(__name__)

DIGEST_JOB_ID = "iris-quiet-hours-digest"


class WatcherService:
    def __init__(
        self,
        definitions_path: str | Path,
        state_path: str | Path,
        notifiers: dict[str, Notifier],
        *,
        quiet_hours: QuietHours | None = None,
        inbox: InboxNotifier | None = None,
        clock: Any = None,
        context: WatcherContext | None = None,
    ) -> None:
        self.definitions_path = Path(definitions_path)
        self.state_path = Path(state_path)
        self.notifiers = dict(notifiers)
        self.inbox = inbox
        if self.inbox is not None and "inbox" not in self.notifiers:
            self.notifiers["inbox"] = self.inbox
        self.quiet_hours = quiet_hours or QuietHours()
        self.context = context or WatcherContext()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._definitions: dict[str, WatcherDefinition] = {}
        self._states: dict[str, WatcherState] = {}
        self._digest: list[Notification] = []
        self._lock = threading.RLock()
        self._scheduler: Any = None
        self.load()
        self._loaded_stamp = self._definitions_stamp()
        self.paused = bool(self._read_json(self.state_path).get("paused", False))


    def load(self) -> None:
        with self._lock:
            self._definitions = {}
            self._states = {}
            for payload in self._read_json_list(self.definitions_path):
                try:
                    definition = WatcherDefinition.from_json(payload)
                except (KeyError, TypeError, ValueError) as error:
                    logger.warning("Skipping a watcher definition: %s", error)
                    continue
                self._definitions[definition.id] = definition
            raw_state = self._read_json(self.state_path)
            for watcher_id, payload in (raw_state.get("watchers", {}) if isinstance(raw_state, dict) else {}).items():
                if watcher_id in self._definitions and isinstance(payload, dict):
                    self._states[watcher_id] = WatcherState.from_json(payload)
            for watcher_id in self._definitions:
                self._states.setdefault(watcher_id, WatcherState())

    def reload_if_changed(self) -> bool:
        stamp = self._definitions_stamp()
        if stamp == self._loaded_stamp:
            return False
        self.load()
        self._loaded_stamp = stamp
        if self._scheduler is not None:
            for definition in self.definitions():
                if definition.enabled:
                    self._schedule(definition)
                else:
                    self._unschedule(definition.id)
        return True

    def _definitions_stamp(self) -> tuple[float, int] | None:
        try:
            stat = self.definitions_path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)

    def save(self) -> None:
        with self._lock:
            self.definitions_path.parent.mkdir(parents=True, exist_ok=True)
            self.definitions_path.write_text(
                json.dumps([item.to_json() for item in self._definitions.values()], indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self._save_state()

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"watchers": {watcher_id: state.to_json() for watcher_id, state in self._states.items()}, "paused": self.paused}
        self.state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    @staticmethod
    def _read_json_list(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning("Could not read %s: %s", path, error)
            return []
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}


    def definitions(self) -> list[WatcherDefinition]:
        with self._lock:
            return sorted(self._definitions.values(), key=lambda item: item.created_at)

    def get(self, watcher_id: str) -> WatcherDefinition | None:
        with self._lock:
            if watcher_id in self._definitions:
                return self._definitions[watcher_id]
            matches = [item for key, item in self._definitions.items() if key.startswith(watcher_id)]
            return matches[0] if len(matches) == 1 else None

    def state(self, watcher_id: str) -> WatcherState | None:
        with self._lock:
            return self._states.get(watcher_id)

    def add(self, definition: WatcherDefinition) -> WatcherDefinition:
        if definition.kind not in KINDS:
            raise ValueError(f"Unknown watcher kind: {definition.kind}. Known: {', '.join(sorted(KINDS))}")
        unknown = [channel for channel in definition.channels if channel not in self.notifiers]
        if unknown:
            raise ValueError(f"Unknown channel(s): {', '.join(unknown)}. Available: {', '.join(sorted(self.notifiers))}")
        with self._lock:
            self._definitions[definition.id] = definition
            self._states[definition.id] = WatcherState()
            self.save()
        self._schedule(definition)
        return definition

    def remove(self, watcher_id: str) -> WatcherDefinition | None:
        definition = self.get(watcher_id)
        if definition is None:
            return None
        with self._lock:
            self._definitions.pop(definition.id, None)
            self._states.pop(definition.id, None)
            self.save()
        self._unschedule(definition.id)
        return definition

    def set_enabled(self, watcher_id: str, enabled: bool) -> WatcherDefinition | None:
        definition = self.get(watcher_id)
        if definition is None:
            return None
        updated = WatcherDefinition(**{**definition.__dict__, "enabled": enabled})
        with self._lock:
            self._definitions[updated.id] = updated
            self.save()
        if enabled:
            self._schedule(updated)
        else:
            self._unschedule(updated.id)
        return updated


    def start(self) -> None:
        if self._scheduler is not None:
            return
        #! @allow-local-import
        from apscheduler.schedulers.background import BackgroundScheduler
        #! @allow-local-import
        from apscheduler.triggers.interval import IntervalTrigger

        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.start()
        for definition in self.definitions():
            if definition.enabled:
                self._schedule(definition)
        self._scheduler.add_job(
            self._flush_digest_if_due, IntervalTrigger(seconds=60), id=DIGEST_JOB_ID, replace_existing=True, coalesce=True, max_instances=1
        )
        logger.info("Watchers started: %d defined", len(self._definitions))

    def stop(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception as error:
                logger.warning("Scheduler shutdown: %s", error)

    @property
    def running(self) -> bool:
        return self._scheduler is not None

    def _schedule(self, definition: WatcherDefinition) -> None:
        if self._scheduler is None or not definition.enabled:
            return
        #! @allow-local-import
        from apscheduler.triggers.interval import IntervalTrigger

        self._scheduler.add_job(
            self.evaluate,
            IntervalTrigger(seconds=max(5, int(definition.interval_seconds))),
            args=[definition.id],
            id=f"watcher-{definition.id}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=max(30, int(definition.interval_seconds)),
        )

    def _unschedule(self, watcher_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(f"watcher-{watcher_id}")
        except Exception:
            pass


    def pause(self) -> None:
        with self._lock:
            self.paused = True
            self._save_state()

    def resume(self) -> None:
        with self._lock:
            self.paused = False
            self._save_state()

    def evaluate_all(self) -> list[Notification]:
        sent: list[Notification] = []
        for definition in self.definitions():
            if definition.enabled:
                sent.extend(self.evaluate(definition.id))
        return sent

    def evaluate(self, watcher_id: str) -> list[Notification]:
        definition = self.get(watcher_id)
        if definition is None or self.paused:
            return []
        with self._lock:
            state = self._states.setdefault(definition.id, WatcherState())
            baseline = state.baseline
        now = self.clock()
        try:
            result = run_check(definition.kind, definition.params, baseline, self.context)
        except Exception as error:
            with self._lock:
                state.failures += 1
                state.last_error = str(error)
                state.last_checked = now.isoformat(timespec="seconds")
                self._save_state()
            logger.warning("Watcher %s failed: %s", definition.label, error)
            return []
        return self._apply(definition, state, result, now)

    def _apply(self, definition: WatcherDefinition, state: WatcherState, result: CheckResult, now: datetime) -> list[Notification]:
        kind = KINDS[definition.kind]
        notifications: list[Notification] = []
        with self._lock:
            state.failures = 0
            state.last_error = None
            state.last_checked = now.isoformat(timespec="seconds")
            state.last_summary = result.summary
            first_run = kind.needs_baseline and state.baseline is None
            was_active = state.active
            triggered = result.triggered
            if was_active and not triggered and result.clear_when is not None:
                try:
                    if not result.clear_when(result.value):
                        triggered = True
                except Exception:
                    pass
            if first_run:
                triggered = False
            state.active = triggered
            if triggered:
                due = state.last_notified is None or (
                    now - datetime.fromisoformat(state.last_notified) >= timedelta(minutes=definition.renotify_minutes)
                )
                if not was_active or due:
                    notifications.append(
                        Notification(watcher_id=definition.id, title=definition.label, body=result.summary, kind=definition.kind, created_at=now.isoformat(timespec="seconds"))
                    )
                    state.last_notified = now.isoformat(timespec="seconds")
                    state.notifications += 1
                    state.last_value = result.value
            elif was_active and definition.notify_on_clear:
                notifications.append(
                    Notification(
                        watcher_id=definition.id,
                        title=f"{definition.label} — cleared",
                        body=result.summary,
                        kind=definition.kind,
                        created_at=now.isoformat(timespec="seconds"),
                        cleared=True,
                    )
                )
            if kind.needs_baseline and (first_run or result.triggered or state.baseline is None):
                state.baseline = result.value
            self._save_state()
        return [self._deliver(definition, item, now) for item in notifications]


    def _deliver(self, definition: WatcherDefinition, notification: Notification, now: datetime) -> Notification:
        quiet = self.quiet_hours.active(now.astimezone() if now.tzinfo else now)
        delivered: list[str] = []
        for channel in definition.channels:
            notifier = self.notifiers.get(channel)
            if notifier is None:
                continue
            if quiet and channel == "toast":
                continue
            try:
                notifier.send(notification)
                delivered.append(channel)
            except Exception as error:
                logger.warning("Notifier %s failed for %s: %s", channel, definition.label, error)
        final = Notification(**{**notification.__dict__, "delivered_to": tuple(delivered), "deferred": quiet and "toast" in definition.channels})
        if final.deferred:
            with self._lock:
                self._digest.append(final)
        return final

    def missed(self) -> list[Notification]:
        with self._lock:
            return list(self._digest)

    def _flush_digest_if_due(self) -> None:
        now = self.clock()
        if self.quiet_hours.active(now.astimezone() if now.tzinfo else now):
            return
        self.flush_digest()

    def flush_digest(self) -> Notification | None:
        with self._lock:
            if not self._digest:
                return None
            items = list(self._digest)
            self._digest = []
        lines = [f"- {item.title}: {item.body}" for item in items[-8:]]
        if len(items) > 8:
            lines.append(f"... and {len(items) - 8} more")
        digest = Notification(watcher_id="digest", title=f"While you were away: {len(items)} alert(s)", body="\n".join(lines), kind="digest")
        toast = self.notifiers.get("toast")
        if toast is not None:
            try:
                toast.send(digest)
            except Exception as error:
                logger.warning("Digest toast failed: %s", error)
        return digest


    def describe(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for definition in self.definitions():
            state = self.state(definition.id) or WatcherState()
            rows.append(
                {
                    "id": definition.id,
                    "label": definition.label,
                    "kind": definition.kind,
                    "params": dict(definition.params),
                    "every": format_duration(definition.interval_seconds),
                    "channels": list(definition.channels),
                    "enabled": definition.enabled,
                    "active": state.active,
                    "last_checked": state.last_checked,
                    "last_summary": state.last_summary,
                    "notifications": state.notifications,
                    "last_error": state.last_error,
                }
            )
        return rows

    def kinds(self) -> list[dict[str, Any]]:
        return [{"name": kind.name, "description": kind.description, "params": dict(kind.params)} for kind in KINDS.values()]


def now_iso() -> str:
    return utc_now_iso()
