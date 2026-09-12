# File: core/assistant/backup_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.storage.backups import BackupService

USAGE = "Usage: /backup [list|now|restore [run]]"


class BackupCommandHandler:

    def __init__(self, service: BackupService, output: OutputSink | None = None) -> None:
        self.service = service
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        if text.lower() != "/backup" and not text.lower().startswith("/backup "):
            return False
        parts = text.split(maxsplit=2)
        subcommand = parts[1].lower() if len(parts) > 1 else "list"
        argument = parts[2].strip() if len(parts) > 2 else ""

        if subcommand == "list":
            self._list()
        elif subcommand == "now":
            report = self.service.run()
            emit_output(self.output, report.summary)
        elif subcommand == "restore":
            self._restore(argument)
        else:
            emit_output(self.output, USAGE)
        return True

    def _list(self) -> None:
        rows = self.service.describe()
        if not rows:
            emit_output(self.output, f"No backups yet under {self.service.destination}. /backup now takes one.")
            return
        lines = [f"{len(rows)} backup run(s) under {self.service.destination} (keeping {self.service.keep}):"]
        for row in rows:
            what = ", ".join(row["databases"] + row["folders"])
            lines.append(f"- {row['run']}  {row['size_bytes'] / 1024 / 1024:.1f} MB  {what}")
        emit_output(self.output, "\n".join(lines))

    def _restore(self, argument: str) -> None:
        try:
            report = self.service.restore(argument or None)
        except FileNotFoundError as error:
            emit_output(self.output, str(error))
            return
        emit_output(self.output, report.summary + "\nRestart Iris so every part reads the restored copies.")
