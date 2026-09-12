# File: core/assistant/changes_command.py

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.actions.changes import ChangeLedger
from core.assistant.output import OutputSink, emit_output

USAGE = "Usage: /changes [n]  ·  /undo [change id]"


class ChangesCommandHandler:

    def __init__(self, ledger: ChangeLedger, output: OutputSink | None = None, on_undo: Any = None) -> None:
        self.ledger = ledger
        self.output = output
        self.on_undo = on_undo

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        lowered = text.lower()
        if lowered == "/changes" or lowered.startswith("/changes "):
            argument = text.split(maxsplit=1)[1].strip() if " " in text else ""
            self._list(int(argument) if argument.isdigit() else 10)
            return True
        if lowered == "/undo" or lowered.startswith("/undo "):
            argument = text.split(maxsplit=1)[1].strip() if " " in text else ""
            self._undo(argument or None)
            return True
        return False

    def _list(self, limit: int) -> None:
        records = self.ledger.recent(limit=max(1, limit))
        if not records:
            emit_output(self.output, "No file changes have been recorded yet.")
            return
        lines = [f"Last {len(records)} file change(s), newest first:"]
        for record in records:
            files = ", ".join(Path(item).name for item in record.files)
            state = " (undone)" if record.undone else ""
            lines.append(f"- {record.id}  {record.action} -> {files}{state}")
        lines.append("/undo puts the newest one back; /undo <id> a specific one.")
        emit_output(self.output, "\n".join(lines))

    def _undo(self, change_id: str | None) -> None:
        report = self.ledger.undo(change_id)
        if report is None:
            emit_output(self.output, f"No change matches {change_id}." if change_id else "Nothing to undo.")
            return
        if self.on_undo is not None and report.ok:
            try:
                self.on_undo(report)
            except Exception:
                pass
        emit_output(self.output, report.summary + "\nRestart Iris if the change was to config.json.")
