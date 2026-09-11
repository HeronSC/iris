# File: core/assistant/knowledge_commands.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.knowledge import KnowledgeError, MemoryKind, MemoryStatus
from core.knowledge.review import KnowledgeReviewWorkflow

_USAGE = (
    "Usage: /knowledge observe <topic> <what you saw> | outcome <id> <what happened> | "
    "open [topic] | hypothesize <topic> <claim> | "
    "evidence <hypothesis> for|against <id> [note] | testing [topic] | "
    "pending | review | show <id> | why <id> | "
    "approve <id> [note] | decline <id> <reason> | topics | embeddings [index]"
)

_TOPIC_NUDGE = (
    "Topics read best as domain/subtopic, like trading/candidates. "
    "It is the main filter when recalling, so it is worth keeping consistent."
)


class KnowledgeCommandHandler:
    def __init__(
        self,
        workflow: KnowledgeReviewWorkflow,
        output: OutputSink | None = None,
        actor: str = "user",
        embeddings: Any | None = None,
    ) -> None:
        self.workflow = workflow
        self.output = output
        self.actor = actor
        self.embeddings = embeddings

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
        if command == "observe":
            return self._observe(argument, rest)
        if command == "outcome":
            return self._outcome(argument, rest)
        if command == "open":
            return self._open(argument)
        if command in {"hypothesize", "hypothesise"}:
            return self._hypothesize(argument, rest)
        if command == "evidence":
            return self._evidence(argument, rest)
        if command == "testing":
            return self._testing(argument)
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
        if command == "embeddings":
            return self._embeddings(argument)

        emit_output(self.output, _USAGE)
        return True

    def _embeddings(self, argument: str) -> bool:
        if self.embeddings is None:
            emit_output(self.output, "Embedding retrieval is not configured.")
            return True
        if argument.lower() == "index":
            stored = self.embeddings.index_pending()
            emit_output(self.output, f"Embedded {stored} records.")
        status = self.embeddings.status()
        if not status["available"]:
            reason = "disabled in config" if not status["enabled"] else "sqlite-vec or the embedding model is unavailable"
            emit_output(self.output, f"Embedding retrieval is off: {reason}.")
            return True
        lines = [
            f"Embedding retrieval: on, {status['model']} ({status['dimensions']} dims)",
            f"- {status['vectors']} vectors stored, {status['pending']} records waiting",
        ]
        if status["last_error"]:
            lines.append(f"- last error: {status['last_error']}")
        if status["pending"]:
            lines.append("- run /knowledge embeddings index to embed them now")
        emit_output(self.output, "\n".join(lines))
        return True

    def _observe(self, topic: str, content: str) -> bool:
        if not topic or not content:
            emit_output(self.output, "Usage: /knowledge observe <topic> <what you saw>", "error")
            return True

        normalized = topic.strip().lower()
        record = self.workflow.observe(normalized, content, source=f"user:{self.actor}")
        emit_output(self.output, f"Recorded {record.id[:8]} in {normalized}.")
        self._nudge_topic(normalized)
        emit_output(self.output, f"Close it later with /knowledge outcome {record.id[:8]} <what happened>")
        return True

    def _hypothesize(self, topic: str, content: str) -> bool:
        if not topic or not content:
            emit_output(self.output, "Usage: /knowledge hypothesize <topic> <claim>", "error")
            return True

        normalized = topic.strip().lower()
        record = self.workflow.hypothesize(normalized, content, source=f"user:{self.actor}")
        emit_output(self.output, f"Proposed {record.id[:8]} in {normalized}. Nothing acts on it yet.")
        self._nudge_topic(normalized)
        emit_output(
            self.output,
            f"Aim evidence at it with /knowledge evidence {record.id[:8]} for|against <id>",
        )
        return True

    def _evidence(self, argument: str, rest: str) -> bool:
        direction, _, tail = rest.partition(" ")
        evidence_id, _, note = tail.strip().partition(" ")
        direction = direction.strip().lower()
        if not argument or direction not in {"for", "against"} or not evidence_id:
            emit_output(
                self.output,
                "Usage: /knowledge evidence <hypothesis> for|against <id> [note]",
                "error",
            )
            return True

        hypothesis = self._resolve(argument)
        if hypothesis is None:
            return True
        evidence = self._resolve(evidence_id)
        if evidence is None:
            return True

        assessment = self.workflow.attach_evidence(
            hypothesis.id,
            evidence.id,
            supports=direction == "for",
            source=f"user:{self.actor}",
            note=note.strip() or None,
        )
        emit_output(self.output, f"Filed {evidence.id[:8]} {direction} {hypothesis.id[:8]}.")
        emit_output(self.output, f"  {hypothesis.content}")
        emit_output(self.output, f"  {assessment.rationale}")
        if assessment.would_change:
            emit_output(
                self.output,
                f"It stays {assessment.status.value}: a settled hypothesis is reopened by a person, "
                "not by the arithmetic.",
            )
        elif assessment.status is MemoryStatus.SUPPORTED:
            emit_output(self.output, "It is now waiting for approval; see /knowledge pending.")
        return True

    def _testing(self, topic: str) -> bool:
        found = self.workflow.under_test(topic=topic.strip().lower() or None)
        if not found:
            emit_output(self.output, "No hypotheses are under test.")
            return True
        emit_output(self.output, f"{len(found)} hypothesis(es) still gathering evidence:")
        for item in found:
            emit_output(self.output, item.summary())
        return True

    def _nudge_topic(self, topic: str) -> None:
        if "/" not in topic:
            emit_output(self.output, _TOPIC_NUDGE)

    def _outcome(self, argument: str, content: str) -> bool:
        if not argument or not content:
            emit_output(self.output, "Usage: /knowledge outcome <id> <what happened>", "error")
            return True
        record = self._resolve(argument)
        if record is None:
            return True
        outcome = self.workflow.close(record.id, content, source=f"user:{self.actor}")
        emit_output(self.output, f"Recorded {outcome.id[:8]} as the outcome of {record.id[:8]}.")
        emit_output(self.output, f"  {record.content}")
        emit_output(self.output, f"  -> {outcome.content}")
        return True

    def _open(self, topic: str) -> bool:
        found = self.workflow.open_observations(topic=topic.strip().lower() or None)
        if not found:
            emit_output(self.output, "No observations are waiting on an outcome.")
            return True
        emit_output(self.output, f"{len(found)} observation(s) still open:")
        for record in found:
            emit_output(self.output, f"{record.id[:8]}  {record.topic}\n  {record.content}")
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
        record = self._resolve(argument)
        if record is None:
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
        found = self.workflow.matches(argument)
        if not found:
            emit_output(self.output, f"Nothing matches '{argument}'.", "error")
            return None
        if len(found) > 1:
            emit_output(self.output, f"'{argument}' is ambiguous; type more of the id:", "error")
            for record in found:
                emit_output(self.output, f"  {record.id[:12]}  {record.kind.value}  {record.content[:60]}")
            return None
        return found[0]
