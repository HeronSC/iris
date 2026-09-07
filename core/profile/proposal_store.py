from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from core.profile.proposal import MemoryProposal


class ProposalStoreError(Exception):
    pass


class MemoryProposalStore:
    def __init__(self, proposals_folder: str | Path) -> None:
        self.proposals_folder = Path(proposals_folder).expanduser()
        self.proposals_folder.mkdir(parents=True, exist_ok=True)
        self.index_path = self.proposals_folder / "pending.json"
        self._ensure_index()

    def add(self, proposal: MemoryProposal) -> None:
        proposals = self._read_proposals()
        if any(item.get("id") == proposal.id for item in proposals):
            raise ProposalStoreError(f"Proposal already exists: {proposal.id}")
        proposals.append(proposal.to_dict())
        self._write_proposals(proposals)

    def get(self, proposal_id: str) -> MemoryProposal | None:
        for payload in self._read_proposals():
            if payload.get("id") == proposal_id:
                return MemoryProposal.from_dict(payload)
        return None

    def list_pending(self) -> list[MemoryProposal]:
        return [MemoryProposal.from_dict(item) for item in self._read_proposals() if str(item.get("status", "pending")).lower() == "pending"]

    def approve(self, proposal_id: str) -> MemoryProposal:
        return self._set_status(proposal_id, "approved")

    def reject(self, proposal_id: str) -> MemoryProposal:
        return self._set_status(proposal_id, "rejected")

    def clear_resolved(self) -> None:
        proposals = [item for item in self._read_proposals() if str(item.get("status", "pending")).lower() == "pending"]
        self._write_proposals(proposals)

    def list_all(self) -> list[MemoryProposal]:
        return [MemoryProposal.from_dict(item) for item in self._read_proposals()]

    def _ensure_index(self) -> None:
        if not self.index_path.exists():
            self._write_proposals([])

    def _read_proposals(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        try:
            with self.index_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError as error:
            raise ProposalStoreError(f"Invalid proposal JSON: {error}") from error
        if not isinstance(payload, dict):
            raise ProposalStoreError("Proposal store must contain a JSON object")
        proposals = payload.get("proposals", [])
        if not isinstance(proposals, list):
            raise ProposalStoreError("Proposal store proposals field must be a list")
        return proposals

    def _write_proposals(self, proposals: list[dict[str, Any]]) -> None:
        payload = {"proposals": proposals}
        self._atomic_write_json(self.index_path, payload)

    def _set_status(self, proposal_id: str, status: str) -> MemoryProposal:
        proposals = self._read_proposals()
        for item in proposals:
            if item.get("id") == proposal_id:
                item["status"] = status
                self._write_proposals(proposals)
                return MemoryProposal.from_dict(item)
        raise ProposalStoreError(f"Proposal not found: {proposal_id}")

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)

