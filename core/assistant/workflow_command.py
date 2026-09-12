# File: core/assistant/workflow_command.py

from __future__ import annotations

import json
from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.workflows.models import WorkflowDefinition, WorkflowError, WorkflowStep, WorkflowTrigger
from core.workflows.service import WorkflowService

USAGE = (
    "Usage: /workflow [list|show <id>|run <id> [key=value ...]|dry-run <id>|approve <run>|runs [id]|"
    "new <name> <tool> [tool ...]|enable <id>|disable <id>|rebase <id>|remove <id>|reload]"
)


class WorkflowCommandHandler:

    def __init__(self, service: WorkflowService, output: OutputSink | None = None) -> None:
        self.service = service
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        _ = state
        text = user_input.strip()
        lowered = text.lower()
        if lowered not in {"/workflow", "/workflows"} and not lowered.startswith(("/workflow ", "/workflows ")):
            return False
        parts = text.split(maxsplit=2)
        subcommand = parts[1].lower() if len(parts) > 1 else "list"
        argument = parts[2].strip() if len(parts) > 2 else ""
        handler = {
            "list": self._list,
            "show": self._show,
            "run": self._run,
            "dry-run": self._dry_run,
            "dryrun": self._dry_run,
            "approve": self._approve,
            "runs": self._runs,
            "new": self._new,
            "enable": lambda arg: self._set_enabled(arg, True),
            "disable": lambda arg: self._set_enabled(arg, False),
            "rebase": self._rebase,
            "remove": self._remove,
            "reload": self._reload,
        }.get(subcommand)
        if handler is None:
            emit_output(self.output, USAGE)
            return True
        handler(argument)
        return True

    def _list(self, _argument: str) -> None:
        rows = self.service.describe()
        if not rows:
            emit_output(self.output, f"No workflows yet. /workflow new <name> <tool> [tool ...] writes one to {self.service.folder} to edit.")
            return
        lines = [f"{len(rows)} workflow(s) in {self.service.folder}:"]
        for row in rows:
            flags = [f"v{row['version']}", row["trigger"]]
            if not row["enabled"]:
                flags.append("disabled")
            if row["drift"]:
                flags.append("tools changed: " + ", ".join(f"{tool} {pair[0]}->{pair[1] or 'gone'}" for tool, pair in row["drift"].items()))
            lines.append(f"- {row['id']}  {row['name']} [{', '.join(flags)}]  {' -> '.join(row['steps'])}")
            if row["last_run"]:
                lines.append(f"  last: {row['last_run']}")
        waiting = self.service.awaiting()
        if waiting:
            lines.append(f"{len(waiting)} run(s) waiting on approval: " + ", ".join(item.id for item in waiting))
        emit_output(self.output, "\n".join(lines))

    def _show(self, argument: str) -> None:
        definition = self._definition(argument)
        if definition is None:
            return
        emit_output(self.output, json.dumps(definition.to_json(), indent=2, ensure_ascii=False))

    def _run(self, argument: str) -> None:
        pieces = argument.split()
        if not pieces:
            emit_output(self.output, "Usage: /workflow run <id> [key=value ...]")
            return
        payload = dict(piece.split("=", 1) for piece in pieces[1:] if "=" in piece)
        run = self.service.run(pieces[0], payload=payload)
        if run is None:
            emit_output(self.output, f"No workflow matches {pieces[0]} (or it is disabled).")
            return
        emit_output(self.output, self._render_run(run))

    def _dry_run(self, argument: str) -> None:
        if not argument:
            emit_output(self.output, "Usage: /workflow dry-run <id>")
            return
        run = self.service.run(argument.split()[0], dry_run=True)
        if run is None:
            emit_output(self.output, f"No workflow matches {argument}.")
            return
        emit_output(self.output, self._render_run(run))

    def _approve(self, argument: str) -> None:
        if not argument:
            waiting = self.service.awaiting()
            emit_output(self.output, "Waiting on approval: " + (", ".join(f"{item.id} ({item.workflow_name})" for item in waiting) or "nothing"))
            return
        run = self.service.approve(argument.split()[0])
        if run is None:
            emit_output(self.output, f"No run waiting on approval matches {argument}.")
            return
        emit_output(self.output, self._render_run(run))

    def _runs(self, argument: str) -> None:
        definition = self.service.get(argument) if argument else None
        rows = self.service.runs(limit=10, workflow_id=definition.id if definition else None)
        if not rows:
            emit_output(self.output, "No workflow runs yet.")
            return
        emit_output(self.output, "\n".join(f"- {item.id}  {item.started_at}  {item.summary}" for item in rows))

    def _new(self, argument: str) -> None:
        pieces = argument.split()
        if len(pieces) < 2:
            emit_output(self.output, "Usage: /workflow new <name> <tool> [tool ...]")
            return
        name, tools = pieces[0], pieces[1:]
        try:
            definition = self.service.save(
                WorkflowDefinition(
                    name=name,
                    description="Edit this file: arguments take {{trigger.key}} and {{steps.<id>.first.<field>}}.",
                    trigger=WorkflowTrigger(),
                    steps=tuple(WorkflowStep(id=f"step{index + 1}", tool=tool) for index, tool in enumerate(tools)),
                ),
                bump=False,
            )
        except WorkflowError as error:
            emit_output(self.output, str(error))
            return
        emit_output(self.output, f"Wrote {definition.name} to {self.service.folder / (definition.id + '.json')}. Edit the arguments and trigger there; /workflow reload picks it up.")

    def _set_enabled(self, argument: str, enabled: bool) -> None:
        definition = self.service.set_enabled(argument, enabled) if argument else None
        if definition is None:
            emit_output(self.output, f"No workflow matches {argument}.")
            return
        emit_output(self.output, f"{definition.name} is {'on' if enabled else 'off'}.")

    def _rebase(self, argument: str) -> None:
        definition = self.service.rebase(argument) if argument else None
        if definition is None:
            emit_output(self.output, f"No workflow matches {argument}.")
            return
        emit_output(self.output, f"{definition.name} now pins {', '.join(f'{tool} {version}' for tool, version in definition.tool_versions.items()) or 'no tools'} (v{definition.version}).")

    def _remove(self, argument: str) -> None:
        definition = self.service.remove(argument) if argument else None
        emit_output(self.output, f"Removed {definition.name}." if definition else f"No workflow matches {argument}.")

    def _reload(self, _argument: str) -> None:
        problems = self.service.load()
        emit_output(self.output, f"Loaded {len(self.service.definitions())} workflow(s)." + ("".join(f"\n- skipped {item}" for item in problems)))

    def _definition(self, argument: str) -> WorkflowDefinition | None:
        definition = self.service.get(argument) if argument else None
        if definition is None:
            emit_output(self.output, f"No workflow matches {argument}." if argument else USAGE)
        return definition

    @staticmethod
    def _render_run(run: Any) -> str:
        lines = [run.summary]
        for step in run.steps:
            detail = step.preview or step.message or step.error or ""
            first = detail.strip().splitlines()[0] if detail.strip() else ""
            lines.append(f"- {step.step_id} ({step.tool}): {step.status}" + (f" — {first[:160]}" if first else "") + (f" [{step.attempts} attempts]" if step.attempts > 1 else ""))
        if run.awaiting:
            lines.append(f"/workflow approve {run.id}")
        return "\n".join(lines)
