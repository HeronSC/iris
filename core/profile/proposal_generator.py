from __future__ import annotations

import json
from typing import Any

from core.conversation.session import ConversationSession
from core.profile.proposal import MemoryProposal, ProposalValidationError


class MemoryProposalGenerator:
    def __init__(self, llm_client: Any | None = None) -> None:
        self.llm_client = llm_client
        self.last_error: str | None = None

    def generate_proposals(self, session: ConversationSession, existing_memory: dict[str, Any]) -> list[MemoryProposal]:
        self.last_error = None
        messages = session.get_messages()
        if not messages:
            return []

        if self.llm_client is None:
            return []

        try:
            prompt = self._build_prompt(messages, existing_memory)
            raw = self.llm_client.generate("You are a memory proposal generator.", prompt)
            payload = json.loads(raw)
            return self._parse_payload(payload)
        except (json.JSONDecodeError, TypeError, ValueError, ProposalValidationError) as error:
            self.last_error = str(error)
            return []

    def _build_prompt(self, messages: list[dict[str, Any]], existing_memory: dict[str, Any]) -> str:
        return json.dumps({"messages": messages[-8:], "existing_memory": existing_memory}, ensure_ascii=False)

    def _parse_payload(self, payload: Any) -> list[MemoryProposal]:
        if not isinstance(payload, dict):
            raise ProposalValidationError("Proposal payload must be a JSON object")
        proposals = payload.get("proposals", [])
        if not isinstance(proposals, list):
            raise ProposalValidationError("Proposal payload proposals field must be a list")
        results: list[MemoryProposal] = []
        for item in proposals:
            if not isinstance(item, dict):
                continue
            proposal = MemoryProposal.from_dict(item)
            self._validate(proposal)
            results.append(proposal)
        return results

    def _validate(self, proposal: MemoryProposal) -> None:
        if proposal.memory_area not in {"profile", "preferences", "projects", "knowledge"}:
            raise ProposalValidationError("Unsupported memory area")
        if proposal.operation not in {"add", "update", "archive"}:
            raise ProposalValidationError("Unsupported operation")
        if not proposal.target_id:
            raise ProposalValidationError("Missing target_id")
        if not proposal.reason:
            raise ProposalValidationError("Missing reason")
        if not proposal.source_message_ids:
            raise ProposalValidationError("Missing source_message_ids")
        if any(not str(item).strip() for item in proposal.source_message_ids):
            raise ProposalValidationError("Invalid source_message_ids")

