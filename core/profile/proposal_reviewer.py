# File: core/profile/proposal_reviewer.py

from __future__ import annotations

from typing import Callable

from core.assistant.output import OutputSink, emit_output
from core.profile.proposal import MemoryProposal


class MemoryProposalReviewer:
    def __init__(self, output: OutputSink | None = None) -> None:
        self.output = output

    def review(self, proposals: list[MemoryProposal], input_func: Callable[[str], str] | None = None) -> list[tuple[MemoryProposal, str]]:
        input_func = input_func or input
        decisions: list[tuple[MemoryProposal, str]] = []
        for index, proposal in enumerate(proposals, start=1):
            emit_output(self.output, f"Pending memory proposal {index} of {len(proposals)}")
            emit_output(self.output, f"Area: {proposal.memory_area}")
            emit_output(self.output, f"Operation: {proposal.operation}")
            emit_output(self.output, f"Proposed memory: {proposal.reason}")
            answer = input_func("Approve? [y/n/s/q]: ").strip().lower()
            if answer in {"y", "yes"}:
                decisions.append((proposal, "approved"))
            elif answer in {"n", "no"}:
                decisions.append((proposal, "rejected"))
            elif answer in {"s", "skip"}:
                decisions.append((proposal, "skipped"))
            else:
                break
        return decisions

