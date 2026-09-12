# File: core/assistant/schedule_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.scheduler.models import JobDefinition
from core.scheduler.service import ScheduleService
from core.watchers.models import parse_duration

USAGE = (
    "Usage: /schedule [list|jobs|run <id>|every <duration> <job> [key=value ...]|"
    "at \"<cron>\" <job> [key=value ...]|enable <id>|disable <id>|remove <id>]"
)


class ScheduleCommandHandler:

    def __init__(self, service: ScheduleService, output: OutputSink | None = None) -> None:
        self.service = service
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        if text.lower() != "/schedule" and not text.lower().startswith("/schedule "):
            return False
        parts = text.split(maxsplit=2)
        subcommand = parts[1].lower() if len(parts) > 1 else "list"
        argument = parts[2].strip() if len(parts) > 2 else ""

        if subcommand == "list":
            self._list()
        elif subcommand == "jobs":
            emit_output(self.output, "Jobs that can be scheduled: " + (", ".join(self.service.kinds()) or "none"))
        elif subcommand == "run":
            self._run(argument)
        elif subcommand in {"every", "at"}:
            self._add(subcommand, argument)
        elif subcommand in {"enable", "disable"}:
            self._set_enabled(argument, subcommand == "enable")
        elif subcommand in {"remove", "delete"}:
            self._remove(argument)
        else:
            emit_output(self.output, USAGE)
        return True

    def _list(self) -> None:
        rows = self.service.describe()
        if not rows:
            emit_output(self.output, "Nothing is scheduled. /schedule jobs lists what can be.")
            return
        lines = [f"{len(rows)} scheduled job(s):"]
        for row in rows:
            flags = [row["schedule"]]
            if not row["enabled"]:
                flags.append("disabled")
            if row["failures"]:
                flags.append(f"{row['failures']} failure(s)")
            lines.append(f"- {row['id']}  {row['label']} [{', '.join(flags)}]")
            if row["params"]:
                lines.append("  " + ", ".join(f"{key}={value}" for key, value in row["params"].items()))
            if row["last_run"]:
                lines.append(f"  last run {row['last_run']}: {row['last_summary']}")
        emit_output(self.output, "\n".join(lines))

    def _run(self, argument: str) -> None:
        if not argument:
            emit_output(self.output, "Usage: /schedule run <id>")
            return
        result = self.service.run(argument)
        if result is None:
            emit_output(self.output, f"No scheduled job matches {argument}.")
            return
        emit_output(self.output, result.summary)

    def _add(self, subcommand: str, argument: str) -> None:
        if not argument:
            emit_output(self.output, USAGE)
            return
        try:
            when, rest = self._split_schedule(subcommand, argument)
        except ValueError as error:
            emit_output(self.output, str(error))
            return
        pieces = rest.split()
        if not pieces:
            emit_output(self.output, USAGE)
            return
        job = pieces[0]
        params = dict(piece.split("=", 1) for piece in pieces[1:] if "=" in piece)
        try:
            definition = JobDefinition(
                job=job,
                params=params,
                interval_seconds=None if subcommand == "at" else parse_duration(when),
                cron=when if subcommand == "at" else None,
            )
            self.service.add(definition)
        except ValueError as error:
            emit_output(self.output, str(error))
            return
        emit_output(self.output, f"Scheduled {definition.label} {definition.schedule} (id {definition.id}).")

    @staticmethod
    def _split_schedule(subcommand: str, argument: str) -> tuple[str, str]:
        if subcommand == "at":
            if argument.startswith('"'):
                closing = argument.find('"', 1)
                if closing == -1:
                    raise ValueError('A cron expression needs closing quotes: /schedule at "0 6 * * *" <job>')
                return argument[1:closing].strip(), argument[closing + 1:].strip()
            pieces = argument.split(maxsplit=5)
            if len(pieces) < 6:
                raise ValueError('Usage: /schedule at "<minute hour day month day-of-week>" <job>')
            return " ".join(pieces[:5]), pieces[5]
        pieces = argument.split(maxsplit=1)
        if len(pieces) < 2:
            raise ValueError("Usage: /schedule every <duration> <job> [key=value ...]")
        return pieces[0], pieces[1]

    def _set_enabled(self, argument: str, enabled: bool) -> None:
        if not argument:
            emit_output(self.output, f"Usage: /schedule {'enable' if enabled else 'disable'} <id>")
            return
        definition = self.service.set_enabled(argument, enabled)
        if definition is None:
            emit_output(self.output, f"No scheduled job matches {argument}.")
            return
        emit_output(self.output, f"{definition.label} is {'on' if enabled else 'off'}.")

    def _remove(self, argument: str) -> None:
        if not argument:
            emit_output(self.output, "Usage: /schedule remove <id>")
            return
        definition = self.service.remove(argument)
        emit_output(
            self.output,
            f"Removed {definition.label}." if definition is not None else f"No scheduled job matches {argument}.",
        )
