from __future__ import annotations

from pathlib import Path

from core.actions.executor import ActionExecutor
from core.assistant.conversation_synonyms import ConversationSynonymStore
from core.assistant.output import OutputSink, emit_output


class PendingActionManager:
    def __init__(
        self,
        executor: ActionExecutor,
        synonyms: ConversationSynonymStore | None = None,
        phrase_log_path: Path | None = None,
        output: OutputSink | None = None,
    ) -> None:
        self.executor = executor
        self.synonyms = synonyms
        self.phrase_log_path = phrase_log_path
        self.output = output

    def handle(self, user_input: str) -> bool:
        stripped = user_input.strip()
        mapped = self._resolve_pending_response(stripped)

        if stripped == "/confirm" or mapped == "confirm":
            if self.executor.has_pending_confirmation():
                self._log_phrase(stripped, "confirm")
                result = self.executor.confirm_pending()
                emit_output(self.output, result.message)
                return True
            if self.executor.consume_expired_confirmation_notice():
                emit_output(self.output, "Pending action confirmation expired.")
                return True
            return stripped == "/confirm"

        if stripped == "/cancel" or mapped == "cancel":
            if self.executor.has_pending_confirmation():
                self._log_phrase(stripped, "cancel")
                result = self.executor.cancel_pending()
                emit_output(self.output, result.message)
                return True
            return stripped == "/cancel"

        if not self.executor.has_pending_confirmation():
            return False

        if self._looks_like_modification(stripped):
            result = self.executor.cancel_pending()
            emit_output(self.output, f"{result.message} Interpreting your updated request.")
            return False

        description = self.executor.pending_description() or "An action is awaiting confirmation."
        emit_output(
            self.output,
            "You still have an action awaiting confirmation:\n\n"
            f"{description}\n\n"
            "Reply naturally to confirm or cancel, or tell me what to change."
        )
        return True

    def _resolve_pending_response(self, text: str) -> str | None:
        if self.synonyms is not None:
            mapped = self.synonyms.resolve_pending_response(text)
            if mapped is not None:
                return mapped
        lowered = text.lower()
        if lowered in {"yes", "y", "confirm", "okay", "approved", "proceed", "do it", "go ahead"}:
            return "confirm"
        if lowered in {"no", "n", "cancel", "never mind", "nevermind", "forget it"}:
            return "cancel"
        return None

    def _log_phrase(self, phrase: str, mapped_to: str) -> None:
        if self.synonyms is None or self.phrase_log_path is None:
            return
        self.synonyms.log_phrase(self.phrase_log_path, phrase, mapped_to, pending_action=self.executor.pending_description())

    def _looks_like_modification(self, text: str) -> bool:
        lowered = text.lower()
        prefixes = (
            "actually",
            "instead",
            "change that",
            "make that",
            "also",
        )
        if lowered.startswith(prefixes):
            return True
        return ":\\" in text or ("\\" in text and any(token in lowered for token in {"index", "scan", "open", "launch", "remember", "remove", "delete", "add", "set", "update"}))