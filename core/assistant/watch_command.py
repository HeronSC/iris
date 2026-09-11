# File: core/assistant/watch_command.py

from __future__ import annotations

import re
from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.watchers.models import WatcherDefinition, parse_duration
from core.watchers.notify import QuietHours

_USAGE = (
    "Usage: /watch [list] | kinds | add <kind> key=value ... [every=5m] [name=...] [channels=toast,inbox] | "
    "remove <id> | pause <id> | resume <id> | run <id|all> | inbox [n] | missed | quiet [22:00-07:00|off] | test"
)


class WatchCommandHandler:

    def __init__(self, service: Any, output: OutputSink | None = None) -> None:
        self.service = service
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        lowered = text.lower()
        if lowered != "/watch" and not lowered.startswith("/watch "):
            return False
        parts = _tokenize(text[len("/watch") :])
        command = parts[0].lower() if parts else "list"
        arguments = parts[1:]
        try:
            handler = {
                "list": self._list,
                "kinds": self._kinds,
                "add": self._add,
                "remove": self._remove,
                "delete": self._remove,
                "pause": lambda args: self._toggle(args, False),
                "resume": lambda args: self._toggle(args, True),
                "run": self._run,
                "inbox": self._inbox,
                "missed": self._missed,
                "quiet": self._quiet,
                "test": self._test,
            }.get(command)
            if handler is None:
                emit_output(self.output, _USAGE)
                return True
            handler(arguments)
        except ValueError as error:
            emit_output(self.output, str(error), "error")
        return True


    def _list(self, _args: list[str]) -> None:
        rows = self.service.describe()
        if not rows:
            emit_output(self.output, "No watchers defined. Try: /watch kinds")
            return
        lines = [f"{len(rows)} watcher(s), scheduler {'running' if self.service.running else 'stopped'}, quiet hours {self.service.quiet_hours}:"]
        for row in rows:
            flags = []
            if not row["enabled"]:
                flags.append("paused")
            if row["active"]:
                flags.append("ACTIVE")
            if row["last_error"]:
                flags.append(f"error: {row['last_error']}")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            last = f" — last: {row['last_summary']}" if row["last_summary"] else ""
            lines.append(f"- {row['id']} {row['label']} every {row['every']} -> {', '.join(row['channels'])}{suffix}{last}")
        emit_output(self.output, "\n".join(lines))

    def _kinds(self, _args: list[str]) -> None:
        lines = ["Watcher kinds:"]
        for kind in self.service.kinds():
            params = ", ".join(f"{key} ({hint})" for key, hint in kind["params"].items()) or "no parameters"
            lines.append(f"- {kind['name']}: {kind['description']}. {params}")
        emit_output(self.output, "\n".join(lines))

    def _add(self, args: list[str]) -> None:
        if not args:
            raise ValueError("Usage: /watch add <kind> key=value ... [every=5m] [name=...] [channels=toast,inbox]")
        kind = args[0]
        params: dict[str, Any] = {}
        options: dict[str, str] = {}
        for token in args[1:]:
            if "=" not in token:
                raise ValueError(f"Expected key=value, got {token!r}")
            key, value = token.split("=", 1)
            value = value.strip('"')
            if key in {"every", "name", "channels", "renotify"}:
                options[key] = value
            else:
                params[key] = _coerce(value)
        definition = WatcherDefinition(
            kind=kind,
            params=params,
            name=options.get("name", ""),
            interval_seconds=parse_duration(options["every"]) if "every" in options else WatcherDefinition.__dataclass_fields__["interval_seconds"].default,
            channels=tuple(item.strip() for item in options["channels"].split(",") if item.strip()) if "channels" in options else ("toast", "inbox"),
            renotify_minutes=parse_duration(options["renotify"]) / 60 if "renotify" in options else 60.0,
        )
        stored = self.service.add(definition)
        emit_output(self.output, f"Watching: {stored.label} every {options.get('every', '5m')} (id {stored.id}). Run it now with /watch run {stored.id}")

    def _remove(self, args: list[str]) -> None:
        if not args:
            raise ValueError("Usage: /watch remove <id>")
        removed = self.service.remove(args[0])
        emit_output(self.output, f"Removed {removed.label}." if removed else f"No watcher matches {args[0]}.")

    def _toggle(self, args: list[str], enabled: bool) -> None:
        if not args:
            raise ValueError(f"Usage: /watch {'resume' if enabled else 'pause'} <id>")
        updated = self.service.set_enabled(args[0], enabled)
        if updated is None:
            emit_output(self.output, f"No watcher matches {args[0]}.")
        else:
            emit_output(self.output, f"{'Resumed' if enabled else 'Paused'} {updated.label}.")

    def _run(self, args: list[str]) -> None:
        target = args[0] if args else "all"
        if target == "all":
            sent = self.service.evaluate_all()
            rows = self.service.describe()
            lines = [f"Ran {len(rows)} watcher(s); {len(sent)} notification(s)."]
            for row in rows:
                lines.append(f"- {row['label']}: {'ACTIVE' if row['active'] else 'ok'} — {row['last_summary'] or row['last_error'] or 'no result'}")
            emit_output(self.output, "\n".join(lines))
            return
        definition = self.service.get(target)
        if definition is None:
            emit_output(self.output, f"No watcher matches {target}.")
            return
        sent = self.service.evaluate(definition.id)
        state = self.service.state(definition.id)
        summary = (state.last_summary or state.last_error or "no result") if state else "no result"
        verdict = "ACTIVE" if state and state.active else "ok"
        emit_output(self.output, f"{definition.label}: {verdict} — {summary}" + (f" ({len(sent)} notification(s) sent)" if sent else ""))

    def _inbox(self, args: list[str]) -> None:
        limit = int(args[0]) if args and args[0].isdigit() else 10
        inbox = getattr(self.service, "inbox", None)
        items = inbox.recent(limit) if inbox is not None else []
        if not items:
            emit_output(self.output, "No notifications yet.")
            return
        lines = [f"Last {len(items)} notification(s):"]
        for item in items:
            where = ", ".join(item.delivered_to) or "nowhere"
            deferred = " (held for quiet hours)" if item.deferred else ""
            lines.append(f"- {item.created_at[:16].replace('T', ' ')} {item.title}: {item.body[:140]} -> {where}{deferred}")
        emit_output(self.output, "\n".join(lines))

    def _missed(self, _args: list[str]) -> None:
        items = self.service.missed()
        if not items:
            emit_output(self.output, "Nothing was held back during quiet hours.")
            return
        lines = [f"{len(items)} alert(s) held during quiet hours:"]
        lines.extend(f"- {item.created_at[:16].replace('T', ' ')} {item.title}: {item.body[:140]}" for item in items)
        emit_output(self.output, "\n".join(lines))

    def _quiet(self, args: list[str]) -> None:
        if not args:
            emit_output(self.output, f"Quiet hours: {self.service.quiet_hours}")
            return
        if args[0].lower() == "off":
            self.service.quiet_hours = QuietHours()
        else:
            self.service.quiet_hours = QuietHours.parse(args[0])
        emit_output(self.output, f"Quiet hours: {self.service.quiet_hours} (until Iris restarts; set notifications.quiet_hours in config.json to keep it)")

    def _test(self, _args: list[str]) -> None:
        #! @allow-local-import
        from core.watchers.models import Notification

        toast = self.service.notifiers.get("toast")
        if toast is None:
            emit_output(self.output, "No toast channel is configured.")
            return
        toast.send(Notification(watcher_id="test", title="Iris", body="Notifications are working.", kind="test"))
        emit_output(self.output, "Sent a test toast.")


_TOKEN = re.compile(r'(?:[^\s"]+|"[^"]*")+')


def _tokenize(text: str) -> list[str]:
    return [match.group(0).replace('"', "") for match in _TOKEN.finditer(text)]


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
