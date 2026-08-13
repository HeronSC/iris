from __future__ import annotations

from typing import Callable

from core.memory.proposal import MemoryProposal


class MemoryProposalReviewer:
    def __init__(self) -> None:
        pass

    def review(self, proposals: list[MemoryProposal], input_func: Callable[[str], str] | None = None) -> list[tuple[MemoryProposal, str]]:
        input_func = input_func or input
        decisions: list[tuple[MemoryProposal, str]] = []
        for index, proposal in enumerate(proposals, start=1):
            print(f"Pending memory proposal {index} of {len(proposals)}")
            print(f"Area: {proposal.memory_area}")
            print(f"Operation: {proposal.operation}")
            print(f"Proposed memory: {proposal.reason}")
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

