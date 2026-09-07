from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.knowledge import KnowledgeError, MemoryKind, MemoryStatus
from core.knowledge.review import KnowledgeReviewWorkflow

_USAGE = (
    "Usage: /knowledge pending | review | show <id> | why <id> | "
    "approve <id> [note] | decline <id> <reason> | topics"
)


class KnowledgeCommandHandler:
    """The review queue for things Iris has learned but is not yet acting on."""

    def __init__(
        self,
        workflow: KnowledgeReviewWorkflow,
        output: OutputSink | None = None,
        actor: str = "user",
    ) -> None:
        self.workflow = workflow
        self.output = output
        self.actor = actor

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        text = (user_input or "").strip()
        if not text.lower().startswith("/knowledge"):
            return False

        parts = text.split()
        if len(parts) == 1:
            emit_output(self.output, _USAGE)
            return True

        command = parts[1].lower()
        argument = parts[2] if len(parts) > 2 else ""
        rest = text.split(maxsplit=3)[3].strip() if len(text.split(maxsplit=3)) == 4 else ""

        try:
            return self._dispatch(command, argument, rest)
        except KnowledgeError as error:
            emit_output(self.output, str(error), "error")
            return True

    def _dispatch(self, command: str, argument: str, rest: str) -> bool:
        if command in {"pending", "list"}:
            return self._pending(refresh=False)
        if command == "review":
            return self._pending(refresh=True)
        if command == "topics":
            return self._topics()
        if command in {"show", "why"}:
            return self._show(argument, why=command == "why")
        if command == "approve":
            return self._approve(argument, rest)
        if command == "decline":
            return self._decline(argument, rest)

        emit_output(self.output, _USAGE)
        return True

    def _pending(self, *, refresh: bool) -> bool:
        if refresh:
            moved = self.workflow.refresh()
            if moved:
                emit_output(self.output, f"Re-assessed the evidence; {moved} hypothesis(es) changed status.")

        pending = self.workflow.pending()
        if not pending:
            emit_output(self.output, "Nothing is waiting for approval.")
            return True

        emit_output(self.output, f"{len(pending)} hypothesis(es) awaiting approval:")
        for item in pending:
            emit_output(self.output, item.summary())
        emit_output(self.output, "Approve with /knowledge approve <id>, or decline with a reason.")
        return True

    def _topics(self) -> bool:
        counts: dict[str, int] = {}
        for status in MemoryStatus:
            for record in self.workflow.tracker.in_status(status, limit=200):
                counts[record.topic] = counts.get(record.topic, 0) + 1
        if not counts:
            emit_output(self.output, "No hypotheses recorded yet.")
            return True
        for topic, count in sorted(counts.items()):
            emit_output(self.output, f"- {topic}: {count} hypothesis(es)")
        return True

    def _show(self, argument: str, *, why: bool) -> bool:
        if not argument:
            emit_output(self.output, _USAGE)
            return True
        record = self.workflow.find(argument)
        if record is None:
            emit_output(self.output, f"No hypothesis matches '{argument}'.", "error")
            return True

        if why:
            emit_output(self.output, self.workflow.explain(record.id))
            return True

        emit_output(self.output, f"{record.content}")
        emit_output(self.output, f"  topic: {record.topic} | status: {record.status.value} | source: {record.source}")
        if record.kind is MemoryKind.HYPOTHESIS:
            emit_output(self.output, f"  {self.workflow.tracker.assess(record.id).rationale}")
        return True

    def _approve(self, argument: str, note: str) -> bool:
        record = self._resolve(argument)
        if record is None:
            return True
        approved = self.workflow.approve(record.id, approved_by=self.actor, note=note or None)
        emit_output(self.output, f"Approved: {approved.content}")
        emit_output(self.output, "Iris will reason with this from now on.")
        return True

    def _decline(self, argument: str, reason: str) -> bool:
        record = self._resolve(argument)
        if record is None:
            return True
        if not reason:
            emit_output(self.output, "Declining needs a reason: /knowledge decline <id> <reason>", "error")
            return True
        declined = self.workflow.decline(record.id, declined_by=self.actor, reason=reason)
        emit_output(self.output, f"Declined: {declined.content}")
        return True

    def _resolve(self, argument: str):
        if not argument:
            emit_output(self.output, _USAGE)
            return None
        record = self.workflow.find(argument)
        if record is None:
            emit_output(self.output, f"No hypothesis matches '{argument}'.", "error")
        return record
